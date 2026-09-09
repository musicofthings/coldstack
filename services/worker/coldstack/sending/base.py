"""Transport contract.

A *transport* is anything that can put a message on the wire. Two families behave
very differently and must not be confused, so capabilities are explicit:

  Mailbox transports (Gmail OAuth, Microsoft Graph, SMTP+IMAP)
      A real human mailbox. Full RFC-2822 threading, IMAP reply sync, low volume,
      per-mailbox reputation. This is what cold outreach requires.

  ESP transports (Resend, Mailjet, Mailchimp Transactional, SendGrid, Postmark,
                  Brevo, Mailgun, MailerSend, SMTP2GO, SparkPost, SES)
      Bulk relay over a shared or dedicated pool. High volume, great APIs, webhook
      events, no inbox to read from unless inbound parsing is configured. Every one
      of these prohibits unsolicited outreach in its acceptable-use policy.

`TransportCapabilities.cold_outreach_safe` is the flag the campaign policy gate reads.
It is not advice. See `coldstack/sending/policy.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class TransportFamily(str, Enum):
    MAILBOX = "mailbox"
    ESP = "esp"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class TransportCapabilities:
    family: TransportFamily
    cold_outreach_safe: bool
    """False for every ESP. Their AUPs prohibit unsolicited mail and enforce it by
    termination, not by warning. Gated in policy.py, never a soft default."""

    supports_threading: bool
    """Can set In-Reply-To/References so a follow-up lands in the same thread."""

    supports_custom_headers: bool
    supports_reply_ingest: bool
    """Can we READ replies? Mailboxes: yes, via IMAP. ESPs: only with an inbound
    parse route configured, and even then it is a webhook, not a mailbox."""

    supports_batch: bool
    max_batch_size: int = 1
    suggested_daily_cap: int = 40
    requires_verified_domain: bool = True
    default_tracking: bool = False
    notes: str = ""


@dataclass(slots=True)
class SendRequest:
    from_email: str
    from_name: str | None
    to_email: str
    to_name: str | None
    subject: str
    text: str
    html: str | None = None
    reply_to: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)
    idempotency_key: str | None = None


@dataclass(slots=True)
class SendResult:
    ok: bool
    provider_message_id: str | None = None
    message_id_hdr: str | None = None
    thread_id: str | None = None
    error: str | None = None
    retryable: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Transport(Protocol):
    name: str
    caps: TransportCapabilities

    async def send(self, req: SendRequest, *, creds: dict[str, str]) -> SendResult: ...
    async def send_batch(self, reqs: list[SendRequest], *, creds: dict[str, str]) -> list[SendResult]: ...
    async def test(self, creds: dict[str, str]) -> tuple[bool, str]: ...
