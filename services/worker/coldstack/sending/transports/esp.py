"""ESP transports — one generic REST adapter driven by a declarative spec per vendor.

Ten bulk-email providers, all doing the same thing over HTTP with different field
names. Writing ten near-identical client classes is how this layer rots, so the
differences live in data (`EspSpec`) and the behaviour lives in one place.

Every spec here is marked `cold_outreach_safe=False`. That is not a judgement about
the vendor's quality — it is their acceptable-use policy. These transports are for
opt-in newsletters, nurture sequences, warm follow-ups and the app's own system mail.
`coldstack/sending/policy.py` enforces it.

Reply handling: ESPs have no mailbox to read. To get replies back you must configure
that vendor's inbound-parse route (SendGrid Inbound Parse, Mailgun Routes, Postmark
inbound stream, Mailjet Parse API) to POST to /webhooks/inbound/{provider}. Where a
vendor has no inbound product, `supports_reply_ingest=False` and a Reply-To pointing
at a real mailbox is the only way to receive answers.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from ..base import (SendRequest, SendResult, Transport, TransportCapabilities,
                    TransportFamily)

Creds = dict[str, str]


def _addr(email: str, name: str | None) -> str:
    return f"{name} <{email}>" if name else email


def _hdrs(req: SendRequest) -> dict[str, str]:
    h = dict(req.headers)
    if req.in_reply_to:
        h["In-Reply-To"] = req.in_reply_to
    if req.references:
        h["References"] = " ".join(req.references)
    return h


@dataclass(frozen=True, slots=True)
class EspSpec:
    name: str
    url: str | Callable[[Creds], str]
    auth: Callable[[Creds], dict[str, str]]
    payload: Callable[[SendRequest], dict[str, Any]]
    extract_id: Callable[[dict[str, Any]], str | None]
    caps: TransportCapabilities
    cred_fields: tuple[str, ...] = ("api_key",)
    basic_auth: Callable[[Creds], tuple[str, str]] | None = None
    form_encoded: bool = False
    test_url: str | Callable[[Creds], str] | None = None


def _caps(
    *, batch: int = 1, reply_ingest: bool = False, threading: bool = True,
    daily: int = 100_000, notes: str = "",
) -> TransportCapabilities:
    return TransportCapabilities(
        family=TransportFamily.ESP,
        cold_outreach_safe=False,          # AUP, not opinion — see module docstring
        supports_threading=threading,
        supports_custom_headers=True,
        supports_reply_ingest=reply_ingest,
        supports_batch=batch > 1,
        max_batch_size=batch,
        suggested_daily_cap=daily,
        requires_verified_domain=True,
        default_tracking=True,
        notes=notes,
    )


# --------------------------------------------------------------------------- specs

RESEND = EspSpec(
    name="resend",
    url="https://api.resend.com/emails",
    auth=lambda c: {"Authorization": f"Bearer {c['api_key']}"},
    payload=lambda r: {k: v for k, v in {
        "from": _addr(r.from_email, r.from_name),
        "to": [r.to_email],
        "subject": r.subject,
        "text": r.text,
        "html": r.html,
        "reply_to": r.reply_to,
        "headers": _hdrs(r) or None,
        "tags": [{"name": k, "value": v} for k, v in r.tags.items()] or None,
    }.items() if v is not None},
    extract_id=lambda d: d.get("id"),
    caps=_caps(batch=100, notes="Batch endpoint: POST /emails/batch. Clean API, good deliverability for opt-in."),
)

MAILJET = EspSpec(
    name="mailjet",
    url="https://api.mailjet.com/v3.1/send",
    auth=lambda c: {},
    basic_auth=lambda c: (c["api_key"], c["api_secret"]),
    cred_fields=("api_key", "api_secret"),
    payload=lambda r: {"Messages": [{
        "From": {"Email": r.from_email, **({"Name": r.from_name} if r.from_name else {})},
        "To": [{"Email": r.to_email, **({"Name": r.to_name} if r.to_name else {})}],
        "Subject": r.subject,
        "TextPart": r.text,
        **({"HTMLPart": r.html} if r.html else {}),
        **({"ReplyTo": {"Email": r.reply_to}} if r.reply_to else {}),
        **({"Headers": _hdrs(r)} if _hdrs(r) else {}),
        **({"CustomID": r.idempotency_key} if r.idempotency_key else {}),
    }]},
    extract_id=lambda d: (d.get("Messages") or [{}])[0].get("To", [{}])[0].get("MessageUUID"),
    caps=_caps(batch=50, reply_ingest=True, notes="v3.1 Send API, Basic auth with key:secret. Parse API gives inbound."),
)

# Mailchimp has two products. Marketing (audiences/campaigns) is not a sequence sender.
# Transactional = Mandrill, which is what a per-recipient sequence needs.
MAILCHIMP_TRANSACTIONAL = EspSpec(
    name="mailchimp_transactional",
    url="https://mandrillapp.com/api/1.0/messages/send.json",
    auth=lambda c: {},
    payload=lambda r: {
        "key": "__INJECT_KEY__",
        "message": {
            "from_email": r.from_email,
            **({"from_name": r.from_name} if r.from_name else {}),
            "to": [{"email": r.to_email, "type": "to",
                    **({"name": r.to_name} if r.to_name else {})}],
            "subject": r.subject,
            "text": r.text,
            **({"html": r.html} if r.html else {}),
            **({"headers": {**_hdrs(r), **({"Reply-To": r.reply_to} if r.reply_to else {})}}
               if (_hdrs(r) or r.reply_to) else {}),
            "tags": list(r.tags.values()) or [],
        },
    },
    extract_id=lambda d: (d[0].get("_id") if isinstance(d, list) and d else None),
    caps=_caps(batch=1, notes="Mandrill. Key goes in the BODY, not a header. Requires a paid Mailchimp plan + Transactional add-on."),
)

SENDGRID = EspSpec(
    name="sendgrid",
    url="https://api.sendgrid.com/v3/mail/send",
    auth=lambda c: {"Authorization": f"Bearer {c['api_key']}"},
    payload=lambda r: {
        "personalizations": [{
            "to": [{"email": r.to_email, **({"name": r.to_name} if r.to_name else {})}],
            **({"headers": _hdrs(r)} if _hdrs(r) else {}),
        }],
        "from": {"email": r.from_email, **({"name": r.from_name} if r.from_name else {})},
        "subject": r.subject,
        "content": ([{"type": "text/plain", "value": r.text}]
                    + ([{"type": "text/html", "value": r.html}] if r.html else [])),
        **({"reply_to": {"email": r.reply_to}} if r.reply_to else {}),
    },
    extract_id=lambda d: d.get("_headers", {}).get("x-message-id"),
    caps=_caps(batch=1000, reply_ingest=True,
               notes="Aggressive anti-cold-email enforcement; accounts are terminated without warning. Inbound Parse for replies."),
)

POSTMARK = EspSpec(
    name="postmark",
    url="https://api.postmarkapp.com/email",
    auth=lambda c: {"X-Postmark-Server-Token": c["api_key"]},
    payload=lambda r: {
        "From": _addr(r.from_email, r.from_name),
        "To": r.to_email,
        "Subject": r.subject,
        "TextBody": r.text,
        **({"HtmlBody": r.html} if r.html else {}),
        **({"ReplyTo": r.reply_to} if r.reply_to else {}),
        **({"Headers": [{"Name": k, "Value": v} for k, v in _hdrs(r).items()]} if _hdrs(r) else {}),
        "MessageStream": "outbound",
    },
    extract_id=lambda d: d.get("MessageID"),
    caps=_caps(batch=500, reply_ingest=True,
               notes="Strictest AUP of the set — manual review, transactional-first. Excellent inbound streams."),
)

BREVO = EspSpec(
    name="brevo",
    url="https://api.brevo.com/v3/smtp/email",
    auth=lambda c: {"api-key": c["api_key"]},
    payload=lambda r: {
        "sender": {"email": r.from_email, **({"name": r.from_name} if r.from_name else {})},
        "to": [{"email": r.to_email, **({"name": r.to_name} if r.to_name else {})}],
        "subject": r.subject,
        "textContent": r.text,
        **({"htmlContent": r.html} if r.html else {}),
        **({"replyTo": {"email": r.reply_to}} if r.reply_to else {}),
        **({"headers": _hdrs(r)} if _hdrs(r) else {}),
    },
    extract_id=lambda d: d.get("messageId"),
    caps=_caps(batch=1, reply_ingest=True, notes="Ex-Sendinblue. Generous free tier; EU data residency."),
)

MAILGUN = EspSpec(
    name="mailgun",
    url=lambda c: f"https://api.{c.get('region_host', 'mailgun.net')}/v3/{c['domain']}/messages",
    auth=lambda c: {},
    basic_auth=lambda c: ("api", c["api_key"]),
    cred_fields=("api_key", "domain"),
    form_encoded=True,
    payload=lambda r: {
        "from": _addr(r.from_email, r.from_name),
        "to": _addr(r.to_email, r.to_name),
        "subject": r.subject,
        "text": r.text,
        **({"html": r.html} if r.html else {}),
        **({"h:Reply-To": r.reply_to} if r.reply_to else {}),
        **{f"h:{k}": v for k, v in _hdrs(r).items()},
    },
    extract_id=lambda d: d.get("id"),
    caps=_caps(batch=1000, reply_ingest=True,
               notes="Form-encoded, not JSON. EU region uses api.eu.mailgun.net. Routes give inbound."),
)

MAILERSEND = EspSpec(
    name="mailersend",
    url="https://api.mailersend.com/v1/email",
    auth=lambda c: {"Authorization": f"Bearer {c['api_key']}"},
    payload=lambda r: {
        "from": {"email": r.from_email, **({"name": r.from_name} if r.from_name else {})},
        "to": [{"email": r.to_email, **({"name": r.to_name} if r.to_name else {})}],
        "subject": r.subject,
        "text": r.text,
        **({"html": r.html} if r.html else {}),
        **({"reply_to": {"email": r.reply_to}} if r.reply_to else {}),
        **({"headers": [{"name": k, "value": v} for k, v in _hdrs(r).items()]} if _hdrs(r) else {}),
    },
    extract_id=lambda d: d.get("message_id"),
    caps=_caps(batch=500, reply_ingest=True, notes="Bulk endpoint at /bulk-email."),
)

SMTP2GO = EspSpec(
    name="smtp2go",
    url="https://api.smtp2go.com/v3/email/send",
    auth=lambda c: {"X-Smtp2go-Api-Key": c["api_key"]},
    payload=lambda r: {
        "sender": _addr(r.from_email, r.from_name),
        "to": [_addr(r.to_email, r.to_name)],
        "subject": r.subject,
        "text_body": r.text,
        **({"html_body": r.html} if r.html else {}),
        **({"custom_headers": [{"header": k, "value": v} for k, v in
            {**_hdrs(r), **({"Reply-To": r.reply_to} if r.reply_to else {})}.items()]}
           if (_hdrs(r) or r.reply_to) else {}),
    },
    extract_id=lambda d: (d.get("data") or {}).get("email_id"),
    caps=_caps(batch=1, notes="More tolerant AUP than most, still not a cold-email licence."),
)

SPARKPOST = EspSpec(
    name="sparkpost",
    url="https://api.sparkpost.com/api/v1/transmissions",
    auth=lambda c: {"Authorization": c["api_key"]},
    payload=lambda r: {
        "recipients": [{"address": {"email": r.to_email,
                                    **({"name": r.to_name} if r.to_name else {})}}],
        "content": {
            "from": {"email": r.from_email, **({"name": r.from_name} if r.from_name else {})},
            "subject": r.subject,
            "text": r.text,
            **({"html": r.html} if r.html else {}),
            **({"reply_to": r.reply_to} if r.reply_to else {}),
            **({"headers": _hdrs(r)} if _hdrs(r) else {}),
        },
    },
    extract_id=lambda d: (d.get("results") or {}).get("id"),
    caps=_caps(batch=1000, reply_ingest=True, notes="Relay webhooks for inbound."),
)

SPECS: dict[str, EspSpec] = {s.name: s for s in (
    RESEND, MAILJET, MAILCHIMP_TRANSACTIONAL, SENDGRID, POSTMARK,
    BREVO, MAILGUN, MAILERSEND, SMTP2GO, SPARKPOST,
)}


# ---------------------------------------------------------------------- transport

class EspTransport:
    """One class, every ESP. Differences are in the spec, not in branches."""

    def __init__(self, spec: EspSpec) -> None:
        self.spec = spec
        self.name = spec.name
        self.caps = spec.caps

    def _url(self, creds: Creds) -> str:
        u = self.spec.url
        return u(creds) if callable(u) else u

    def _body(self, req: SendRequest, creds: Creds) -> dict[str, Any]:
        body = self.spec.payload(req)
        # Mandrill is the one vendor that authenticates in the body.
        if body.get("key") == "__INJECT_KEY__":
            body["key"] = creds["api_key"]
        return body

    async def send(self, req: SendRequest, *, creds: Creds) -> SendResult:
        missing = [f for f in self.spec.cred_fields if not creds.get(f)]
        if missing:
            return SendResult(ok=False, error=f"missing credential fields: {missing}")

        headers = dict(self.spec.auth(creds))
        auth = httpx.BasicAuth(*self.spec.basic_auth(creds)) if self.spec.basic_auth else None
        body = self._body(req, creds)
        if req.idempotency_key and self.spec.name in ("resend", "postmark"):
            headers["Idempotency-Key"] = req.idempotency_key

        kw: dict[str, Any] = {"data": body} if self.spec.form_encoded else {"json": body}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(self._url(creds), headers=headers, auth=auth, **kw)
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=str(exc), retryable=True)

        try:
            data = resp.json()
        except json.JSONDecodeError:
            data = {"_text": resp.text}

        if resp.status_code >= 400:
            return SendResult(
                ok=False,
                error=f"{self.spec.name} {resp.status_code}: {resp.text[:400]}",
                # 429 and 5xx are worth retrying; 4xx auth/validation are not.
                retryable=resp.status_code == 429 or resp.status_code >= 500,
                raw=data if isinstance(data, dict) else {"body": data},
            )

        payload = data if isinstance(data, dict) else {"body": data}
        payload.setdefault("_headers", dict(resp.headers))
        return SendResult(
            ok=True,
            provider_message_id=self.spec.extract_id(data if isinstance(data, (dict, list)) else {}),
            raw=payload,
        )

    async def send_batch(self, reqs: list[SendRequest], *, creds: Creds) -> list[SendResult]:
        # Per-vendor batch endpoints differ enough to be worth doing individually;
        # sequential-with-concurrency is correct and safe until volume justifies it.
        return [await self.send(r, creds=creds) for r in reqs]

    async def test(self, creds: Creds) -> tuple[bool, str]:
        missing = [f for f in self.spec.cred_fields if not creds.get(f)]
        if missing:
            return False, f"missing: {', '.join(missing)}"
        probe = SendRequest(
            from_email="probe@invalid.invalid", from_name=None,
            to_email="probe@invalid.invalid", to_name=None,
            subject="coldstack credential probe", text="probe",
        )
        res = await self.send(probe, creds=creds)
        # A 401/403 means the key is wrong. Anything else means the key was accepted
        # and the request failed on validation, which is what we want from a probe.
        if res.error and (" 401:" in res.error or " 403:" in res.error):
            return False, "credentials rejected"
        return True, "credentials accepted"


def build_all() -> dict[str, EspTransport]:
    return {name: EspTransport(spec) for name, spec in SPECS.items()}


assert all(isinstance(t, Transport) for t in build_all().values()), "EspTransport must satisfy Transport"
