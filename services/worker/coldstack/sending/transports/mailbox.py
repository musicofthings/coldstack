"""Mailbox transports — the only family allowed for cold outreach.

A real mailbox on a real domain, sending at human volume. Slower and more fiddly than
an ESP, and that is the point: reputation accrues to the mailbox, replies come back
to it over IMAP, and threading is genuine RFC-2822 rather than a vendor's approximation.

Three implementations share one shape:
  smtp        — universal fallback, works with any provider incl. Workspace resellers
  gmail_oauth — Gmail API; best cold deliverability, OAuth refresh handled upstream
  ms_graph    — Microsoft 365; watch per-tenant throttling

Only the SMTP one is implemented here; the two OAuth transports need the token
refresh plumbing from the credential vault and land with P2.
"""
from __future__ import annotations

import smtplib
import ssl
import uuid
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from datetime import datetime, timezone

from ..base import (SendRequest, SendResult, TransportCapabilities,
                    TransportFamily)

MAILBOX_CAPS = TransportCapabilities(
    family=TransportFamily.MAILBOX,
    cold_outreach_safe=True,
    supports_threading=True,
    supports_custom_headers=True,
    supports_reply_ingest=True,
    supports_batch=False,
    max_batch_size=1,
    suggested_daily_cap=40,      # per mailbox, after a full 14-21 day ramp
    requires_verified_domain=True,
    default_tracking=False,      # deliberately off - see docs/04-sending-engine.md
    notes="One human mailbox. Ramp enforced by the scheduler, not by the transport.",
)


class SmtpTransport:
    name = "smtp"
    caps = MAILBOX_CAPS

    async def send(self, req: SendRequest, *, creds: dict[str, str]) -> SendResult:
        msg = EmailMessage()
        msg["From"] = formataddr((req.from_name or "", req.from_email))
        msg["To"] = formataddr((req.to_name or "", req.to_email))
        msg["Subject"] = req.subject
        msg["Date"] = format_datetime(datetime.now(timezone.utc))

        domain = req.from_email.split("@")[-1]
        mid = make_msgid(domain=domain)
        msg["Message-ID"] = mid

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

        host = creds.get("smtp_host", "")
        port = int(creds.get("smtp_port", 587))
        try:
            ctx = ssl.create_default_context()
            if port == 465:
                server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
            else:
                server = smtplib.SMTP(host, port, timeout=30)
                server.starttls(context=ctx)
            with server:
                server.login(creds["smtp_user"], creds["smtp_password"])
                server.send_message(msg)
        except smtplib.SMTPResponseException as exc:
            # 4xx is a transient defer, 5xx is a hard rejection worth surfacing as a bounce.
            return SendResult(ok=False, error=f"{exc.smtp_code} {exc.smtp_error!r}",
                              retryable=400 <= exc.smtp_code < 500)
        except (smtplib.SMTPException, OSError) as exc:
            return SendResult(ok=False, error=str(exc), retryable=True)

        return SendResult(
            ok=True,
            provider_message_id=mid,
            message_id_hdr=mid,
            thread_id=req.references[0] if req.references else mid,
        )

    async def send_batch(self, reqs: list[SendRequest], *, creds: dict[str, str]) -> list[SendResult]:
        # Deliberately not a real batch. Cold sending is paced, one message at a time,
        # with jitter between sends; the scheduler owns that spacing.
        return [await self.send(r, creds=creds) for r in reqs]

    async def test(self, creds: dict[str, str]) -> tuple[bool, str]:
        try:
            port = int(creds.get("smtp_port", 587))
            ctx = ssl.create_default_context()
            if port == 465:
                s = smtplib.SMTP_SSL(creds["smtp_host"], port, context=ctx, timeout=15)
            else:
                s = smtplib.SMTP(creds["smtp_host"], port, timeout=15)
                s.starttls(context=ctx)
            with s:
                s.login(creds["smtp_user"], creds["smtp_password"])
        except Exception as exc:                       # noqa: BLE001 - report anything
            return False, str(exc)
        return True, "SMTP login OK"
