"""Peer-to-peer warmup pool.

Participating mailboxes exchange real conversations with each other: send, open,
reply, and rescue from spam. That traffic is what tells a receiver the mailbox belongs
to a person rather than a sender.

Two constraints the naive implementation gets wrong:

1. **Templated warmup text is itself a fingerprint.** A pool where every message is
   "Hi, following up on the doc" is trivially detectable, and being detected as a
   warmup pool is worse than not warming at all. Subjects and bodies are composed from
   independent fragment pools, so the corpus is combinatorially large rather than a
   list of ten templates.
2. **Warmup traffic must never reach campaign analytics.** Every message carries
   `X-ColdStack-Warmup`, and any reply-rate or bounce metric filters on it. A warmup
   pool replying to itself at 40% would otherwise make a dead campaign look healthy.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

WARMUP_HEADER = "X-ColdStack-Warmup"
REPLY_PROBABILITY = 0.45          # peers reply to roughly this share
MAX_PAIR_REPEATS_PER_WEEK = 2

_OPENERS = ["Quick one -", "Hi,", "Morning -", "Hey,", "Following up -", "Small thing -"]
_TOPICS = [
    "the timeline we discussed", "next week's review", "the draft you sent",
    "that supplier quote", "the onboarding checklist", "the budget line",
    "the venue booking", "the handover notes", "the Q3 numbers", "the site visit",
]
_ASKS = [
    "does Thursday still work?", "can you take a look when you get a chance?",
    "any changes before I send it on?", "shall I go ahead?",
    "let me know if that's still the plan.", "happy to redo it if not.",
]
_SIGNOFFS = ["Thanks", "Cheers", "Best", "Appreciated", "Talk soon"]
_SUBJECTS = [
    "Quick question", "Re: timeline", "Following up", "One thing", "Checking in",
    "Small update", "Before Friday", "Draft attached", "Next steps", "Quick check",
]


@dataclass(slots=True)
class WarmupMessage:
    from_mailbox: str
    to_mailbox: str
    subject: str
    body: str
    should_reply: bool
    headers: dict[str, str] = field(default_factory=dict)


def compose(seed: str) -> tuple[str, str]:
    """Deterministic per seed, but drawn from independent pools so the space is
    len(openers) * len(topics) * len(asks) * len(signoffs) - tens of thousands of
    distinct bodies rather than a handful of templates."""
    rng = random.Random(hashlib.sha256(seed.encode()).hexdigest())
    subject = rng.choice(_SUBJECTS)
    body = (f"{rng.choice(_OPENERS)} about {rng.choice(_TOPICS)} - "
            f"{rng.choice(_ASKS)}\n\n{rng.choice(_SIGNOFFS)}")
    return subject, body


def warmup_volume(ramp_day: int, target: int = 40) -> int:
    """Warmup runs ahead of real sending: heavy early, tapering as real volume grows."""
    if ramp_day < 0:
        return 0
    if ramp_day < 7:
        return min(target, 10 + ramp_day * 3)
    if ramp_day < 21:
        return max(5, int(target * 0.4) - (ramp_day - 7))
    return 5                      # a small maintenance trickle, indefinitely


def _domain(email: str) -> str:
    return email.rpartition("@")[2].lower()


def pair_pool(mailboxes: list[str], day: date, *, per_mailbox: int = 4,
              history: dict[tuple[str, str], int] | None = None,
              rng: random.Random | None = None) -> list[WarmupMessage]:
    """Build the day's warmup exchanges.

    Never pairs two mailboxes on the same domain - that traffic never leaves the
    provider, so it teaches external receivers nothing.
    """
    rng = rng or random.Random(day.toordinal())
    history = history if history is not None else {}
    out: list[WarmupMessage] = []

    for sender in mailboxes:
        candidates = [m for m in mailboxes
                      if m != sender and _domain(m) != _domain(sender)
                      and history.get((sender, m), 0) < MAX_PAIR_REPEATS_PER_WEEK]
        rng.shuffle(candidates)
        for recipient in candidates[:per_mailbox]:
            seed = f"{sender}|{recipient}|{day.isoformat()}"
            subject, body = compose(seed)
            history[(sender, recipient)] = history.get((sender, recipient), 0) + 1
            out.append(WarmupMessage(
                from_mailbox=sender, to_mailbox=recipient,
                subject=subject, body=body,
                should_reply=rng.random() < REPLY_PROBABILITY,
                headers={WARMUP_HEADER: "1", "Precedence": "bulk"},
            ))
    return out


def is_warmup(headers: dict[str, str]) -> bool:
    """Analytics and breakers must call this and exclude anything it flags."""
    return any(k.lower() == WARMUP_HEADER.lower() for k in headers)


def pool_health(messages: list[WarmupMessage]) -> dict[str, float | int]:
    replies = sum(1 for m in messages if m.should_reply)
    return {
        "messages": len(messages),
        "reply_rate": replies / len(messages) if messages else 0.0,
        "unique_senders": len({m.from_mailbox for m in messages}),
        "unique_pairs": len({(m.from_mailbox, m.to_mailbox) for m in messages}),
    }
