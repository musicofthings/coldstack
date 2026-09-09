"""All transports, one place. The UI reads capabilities from here."""
from __future__ import annotations

from .base import Transport
from .transports.esp import build_all as _esps
from .transports.mailbox import SmtpTransport


def all_transports() -> dict[str, Transport]:
    t: dict[str, Transport] = {"smtp": SmtpTransport()}
    t.update(_esps())
    return t


def describe() -> list[dict]:
    """Rows for the 'connect a sender' screen."""
    return [{
        "name": n,
        "family": str(t.caps.family),
        "cold_safe": t.caps.cold_outreach_safe,
        "batch": t.caps.max_batch_size,
        "reply_ingest": t.caps.supports_reply_ingest,
        "notes": t.caps.notes,
    } for n, t in sorted(all_transports().items(), key=lambda kv: (not kv[1].caps.cold_outreach_safe, kv[0]))]
