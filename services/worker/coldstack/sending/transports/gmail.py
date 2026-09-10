"""Gmail transport via a service account with domain-wide delegation.

Why a service account rather than per-mailbox OAuth: Gmail's read scopes are restricted,
so a published consent screen drags in an annual CASA assessment, and staying in Testing
expires refresh tokens after seven days. Delegation is authorised once by the Workspace
admin and never expires. docs/06-mailbox-setup.md has the full reasoning.

One credential (the service account key) covers every mailbox on the domain; the mailbox
is selected per send by `subject`. That is powerful enough to be worth stating plainly:
this key can act as any user on the domain within the granted scopes, which is why
ColdStack keeps it sealed in the vault and why the setup doc argues for a dedicated
outreach domain rather than your primary one.

Threading note: Gmail assigns its own Message-ID on send and ignores one supplied in the
raw MIME. So a follow-up is threaded by passing Gmail's `threadId`, and the real
Message-ID is read back from the sent message so inbound replies can be matched to the
enrollment by header chain.
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import format_datetime, formataddr
from datetime import datetime, timezone
from typing import Any

import httpx

from ..base import SendRequest, SendResult, TransportCapabilities, TransportFamily

API = "https://gmail.googleapis.com/gmail/v1"
SCOPES = ("https://www.googleapis.com/auth/gmail.send",
          "https://www.googleapis.com/auth/gmail.readonly")

GMAIL_CAPS = TransportCapabilities(
    family=TransportFamily.MAILBOX,
    cold_outreach_safe=True,
    supports_threading=True,
    supports_custom_headers=True,
    supports_reply_ingest=True,
    supports_batch=False,
    max_batch_size=1,
    suggested_daily_cap=40,      # Workspace allows 2,000/day; reputation allows ~40
    requires_verified_domain=True,
    default_tracking=False,
    notes="Service account + domain-wide delegation; one key serves every mailbox on the domain.",
)


class TokenError(RuntimeError):
    pass


@dataclass
class DelegatedTokens:
    """Mints and caches a delegated access token per mailbox.

    google-auth handles the signed-JWT grant; everything else here is plain REST, which
    keeps the dependency surface to one library rather than the full API client.
    """
    service_account_info: dict[str, Any]
    scopes: tuple[str, ...] = SCOPES
    _cache: dict[str, tuple[str, float]] = field(default_factory=dict, repr=False)
    skew_seconds: int = 120        # refresh early; a token expiring mid-send is a lost message

    def token(self, subject: str) -> str:
        hit = self._cache.get(subject)
        if hit and hit[1] - self.skew_seconds > time.time():
            return hit[0]

        try:
            from google.oauth2 import service_account
            import google.auth.transport.requests as greq
        except ImportError as exc:                       # pragma: no cover
            raise TokenError("google-auth is required for the Gmail transport") from exc

        try:
            creds = service_account.Credentials.from_service_account_info(
                self.service_account_info, scopes=list(self.scopes), subject=subject)
            creds.refresh(greq.Request())
        except Exception as exc:                          # noqa: BLE001
            raise TokenError(_explain(str(exc), subject)) from exc

        expiry = creds.expiry.replace(tzinfo=timezone.utc).timestamp() if creds.expiry \
            else time.time() + 3000
        self._cache[subject] = (creds.token, expiry)
        return creds.token

    def forget(self, subject: str) -> None:
        self._cache.pop(subject, None)


def _explain(message: str, subject: str) -> str:
    """Google's delegation errors are terse and the two common ones mean very different
    things. Saying which is which saves an afternoon."""
    if "unauthorized_client" in message:
        return (f"delegation not authorised for these scopes. In Workspace Admin → "
                f"Security → API controls → Domain-wide Delegation, confirm the service "
                f"account's numeric Unique ID is listed with exactly: {', '.join(SCOPES)}")
    if "invalid_grant" in message or "Invalid email" in message:
        return (f"delegation is configured, but '{subject}' is not a mailbox on the "
                f"domain. Check the address, or create the mailbox first.")
    return message[:300]


def build_mime(req: SendRequest) -> str:
    msg = EmailMessage()
    msg["From"] = formataddr((req.from_name or "", req.from_email))
    msg["To"] = formataddr((req.to_name or "", req.to_email))
    msg["Subject"] = req.subject
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    if req.reply_to:
        msg["Reply-To"] = req.reply_to
    if req.in_reply_to:
        msg["In-Reply-To"] = req.in_reply_to
    if req.references:
        msg["References"] = " ".join(req.references)
    for k, v in req.headers.items():
        msg[k] = v
    msg.set_content(req.text)
    if req.html:
        msg.add_alternative(req.html, subtype="html")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


class GmailTransport:
    name = "gmail"
    caps = GMAIL_CAPS

    def __init__(self, tokens: DelegatedTokens | None = None) -> None:
        self._tokens = tokens

    def _minter(self, creds: dict[str, Any]) -> DelegatedTokens:
        if self._tokens is not None:
            return self._tokens
        info = creds.get("service_account_info")
        if not info:
            raise TokenError("no service_account_info in the stored credential")
        return DelegatedTokens(service_account_info=info)

    async def send(self, req: SendRequest, *, creds: dict[str, Any]) -> SendResult:
        subject_mailbox = creds.get("subject") or req.from_email
        try:
            token = self._minter(creds).token(subject_mailbox)
        except TokenError as exc:
            # Configuration problems are not transient; retrying just burns quota.
            return SendResult(ok=False, error=str(exc), retryable=False)

        body: dict[str, Any] = {"raw": build_mime(req)}
        if req.in_reply_to and creds.get("thread_id"):
            body["threadId"] = creds["thread_id"]

        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                resp = await client.post(
                    f"{API}/users/{subject_mailbox}/messages/send",
                    headers=headers, json=body)
                if resp.status_code == 401:
                    # Token rejected mid-flight: drop the cache entry and try once more.
                    self._minter(creds).forget(subject_mailbox)
                    token = self._minter(creds).token(subject_mailbox)
                    resp = await client.post(
                        f"{API}/users/{subject_mailbox}/messages/send",
                        headers={"Authorization": f"Bearer {token}"}, json=body)

                if resp.status_code >= 400:
                    return SendResult(
                        ok=False,
                        error=f"gmail {resp.status_code}: {resp.text[:300]}",
                        # 429 and 5xx are worth retrying; 403 is usually quota or a
                        # policy block and repeating it makes things worse.
                        retryable=resp.status_code == 429 or resp.status_code >= 500,
                        raw=_json(resp))

                sent = resp.json()
                header_id = await self._read_message_id(
                    client, subject_mailbox, sent.get("id"), headers)
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=str(exc), retryable=True)

        return SendResult(
            ok=True,
            provider_message_id=sent.get("id"),
            message_id_hdr=header_id,
            thread_id=sent.get("threadId"),
            raw=sent,
        )

    @staticmethod
    async def _read_message_id(client: httpx.AsyncClient, mailbox: str,
                               message_id: str | None, headers: dict[str, str]) -> str | None:
        """Gmail replaces any Message-ID we set, so read back the real one. Without it,
        an inbound reply cannot be matched to its enrollment by header chain."""
        if not message_id:
            return None
        try:
            r = await client.get(f"{API}/users/{mailbox}/messages/{message_id}",
                                 headers=headers,
                                 params={"format": "metadata",
                                         "metadataHeaders": "Message-ID"})
            if r.status_code >= 400:
                return None
            for h in (r.json().get("payload") or {}).get("headers") or []:
                if h.get("name", "").lower() == "message-id":
                    return h.get("value")
        except httpx.HTTPError:
            return None
        return None

    async def send_batch(self, reqs: list[SendRequest], *, creds: dict[str, Any]) -> list[SendResult]:
        # Deliberately serial: cold sending is paced, and the scheduler owns the spacing.
        return [await self.send(r, creds=creds) for r in reqs]

    async def test(self, creds: dict[str, Any]) -> tuple[bool, str]:
        mailbox = creds.get("subject", "")
        if not mailbox:
            return False, "no mailbox address configured"
        try:
            token = self._minter(creds).token(mailbox)
        except TokenError as exc:
            return False, str(exc)
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(f"{API}/users/{mailbox}/profile",
                                     headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            return False, str(exc)
        if r.status_code >= 400:
            return False, f"gmail {r.status_code}: {r.text[:200]}"
        p = r.json()
        return True, (f"{p.get('emailAddress')} reachable "
                      f"({p.get('messagesTotal', 0)} messages in mailbox)")


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        return resp.json()
    except ValueError:
        return {"body": resp.text[:300]}
