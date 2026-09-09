"""Matching an inbound message back to the enrollment that caused it.

Subject matching is not used. "Re: Quick question" matches half the mailbox, people
edit subjects, and mail clients localise the "Re:" prefix. RFC 5322 gave us
Message-ID/References for exactly this, so the chain is the primary key and the
sender address is the fallback.

The fallback matters more than it sounds: bounces and auto-replies frequently arrive
with no References at all, and a DSN's own Message-ID has nothing to do with ours.
For those we match on the recipient the report is *about*, which is why the classifier
extracts Final-Recipient.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import Message

_MSGID = re.compile(r"<[^<>@\s]+@[^<>\s]+>")


def message_ids(header_value: str | None) -> list[str]:
    return _MSGID.findall(header_value or "")


def chain(msg: Message) -> list[str]:
    """Every Message-ID this message points at, most recent first."""
    ids = message_ids(msg.get("In-Reply-To"))
    for mid in reversed(message_ids(msg.get("References"))):
        if mid not in ids:
            ids.append(mid)
    return ids


@dataclass(slots=True)
class Match:
    enrollment_id: str | None
    method: str          # "chain" | "address" | "bounced_address" | "none"
    confidence: float


def match_enrollment(msg: Message, *, sent_index: dict[str, str],
                     address_index: dict[str, str],
                     bounced_address: str | None = None) -> Match:
    """
    sent_index     Message-ID we sent -> enrollment id
    address_index  contact email      -> enrollment id
    """
    for mid in chain(msg):
        if mid in sent_index:
            return Match(sent_index[mid], "chain", 0.99)

    if bounced_address:
        hit = address_index.get(bounced_address.lower())
        if hit:
            return Match(hit, "bounced_address", 0.9)

    sender = _extract_address(msg.get("From") or "")
    if sender:
        hit = address_index.get(sender)
        if hit:
            # Weaker: the person may be replying to something else entirely, or a
            # colleague may be answering from a shared mailbox.
            return Match(hit, "address", 0.7)

    return Match(None, "none", 0.0)


def _extract_address(value: str) -> str | None:
    m = re.search(r"<([^>@\s]+@[^>\s]+)>", value) or re.search(r"([^\s<>]+@[^\s<>]+)", value)
    return m.group(1).lower() if m else None
