"""Send scheduling: ramp, rotation, pacing.

This module is the reason a user's domain survives its first month. It is pure
functions over state so it can be tested exhaustively without sending anything.

Three rules it enforces, none of them advisory:

1. **Ramp.** A new mailbox starts at 5 sends/day and grows ~30%/day to its cap over
   two to three weeks. A fresh domain that sends 500 on day three is dead, and no
   amount of good copy recovers it.
2. **Per-mailbox daily cap.** Google Workspace allows ~2,000/day; cold outreach should
   use ~40. The limit that matters is reputational, not the provider's.
3. **Domain cool-off.** Never hit the same recipient domain twice from one mailbox
   inside a short window - that pattern is what filters look for.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum

RAMP_START = 5
# 1.15, not 1.3. At 30%/day a mailbox reaches a 40/day cap in 8 days, which contradicts
# the 14-21 day ramp this module is supposed to enforce and is roughly twice as fast as
# is safe. 15%/day from 5 reaches 40 on day 15 - matching the documented policy.
RAMP_GROWTH = 1.15
DEFAULT_RAMP_DAYS = 21
DOMAIN_COOLOFF = timedelta(hours=6)
MIN_GAP = timedelta(seconds=90)
MAX_GAP = timedelta(seconds=600)


class MailboxStatus(str, Enum):
    WARMING = "warming"
    HEALTHY = "healthy"
    THROTTLED = "throttled"
    QUARANTINED = "quarantined"
    DISABLED = "disabled"

    def __str__(self) -> str:
        return self.value


def ramp_cap(ramp_day: int, target_cap: int, *, start: int = RAMP_START,
             growth: float = RAMP_GROWTH) -> int:
    """Sends allowed on a given ramp day. Day 0 is the first day of sending."""
    if ramp_day < 0:
        return 0
    allowed = start * (growth ** ramp_day)
    return max(0, min(target_cap, int(allowed)))


def days_to_full_ramp(target_cap: int, *, start: int = RAMP_START,
                      growth: float = RAMP_GROWTH) -> int:
    day = 0
    while ramp_cap(day, target_cap, start=start, growth=growth) < target_cap:
        day += 1
        if day > 365:
            break
    return day


@dataclass(slots=True)
class Mailbox:
    id: str
    email: str
    daily_cap: int = 40
    ramp_started_on: date | None = None
    sent_today: int = 0
    status: MailboxStatus = MailboxStatus.WARMING
    bounce_rate_7d: float = 0.0
    last_sent_at: datetime | None = None
    recent_domains: dict[str, datetime] = field(default_factory=dict)

    def ramp_day(self, today: date) -> int:
        if self.ramp_started_on is None:
            return 0
        return (today - self.ramp_started_on).days

    def allowance(self, today: date) -> int:
        if self.status in (MailboxStatus.QUARANTINED, MailboxStatus.DISABLED):
            return 0
        cap = ramp_cap(self.ramp_day(today), self.daily_cap)
        if self.status is MailboxStatus.THROTTLED:
            cap = cap // 2
        return cap

    def remaining(self, today: date) -> int:
        return max(0, self.allowance(today) - self.sent_today)

    def can_send_to(self, domain: str, now: datetime) -> bool:
        last = self.recent_domains.get(domain.lower())
        return last is None or (now - last) >= DOMAIN_COOLOFF


@dataclass(slots=True)
class SendWindow:
    start_hour: int = 9
    end_hour: int = 17
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)     # Monday..Friday

    def contains(self, when: datetime) -> bool:
        return when.weekday() in self.weekdays and self.start_hour <= when.hour < self.end_hour

    def next_open(self, after: datetime) -> datetime:
        when = after
        for _ in range(14 * 24):                    # two weeks of hourly steps is plenty
            if self.contains(when):
                return when
            if when.weekday() in self.weekdays and when.hour < self.start_hour:
                when = when.replace(hour=self.start_hour, minute=0, second=0, microsecond=0)
                continue
            when = (when + timedelta(days=1)).replace(
                hour=self.start_hour, minute=0, second=0, microsecond=0)
        return when


class NoMailboxAvailable(Exception):
    pass


def pick_mailbox(pool: list[Mailbox], recipient_domain: str, now: datetime,
                 rng: random.Random | None = None) -> Mailbox:
    """Weighted by remaining allowance, so warm mailboxes carry the load and a mailbox
    early in its ramp is not drained on day one."""
    today = now.date()
    # Pacing is part of eligibility, not an afterthought. Handing back a mailbox that
    # is still inside its minimum gap makes the caller defer an enrollment that another
    # mailbox could have taken immediately.
    eligible = [m for m in pool
                if m.remaining(today) > 0
                and m.can_send_to(recipient_domain, now)
                and (m.last_sent_at is None or now - m.last_sent_at >= MIN_GAP)]
    if not eligible:
        raise NoMailboxAvailable(
            f"no mailbox can send to {recipient_domain} right now: "
            f"{len(pool)} in pool, "
            f"{sum(1 for m in pool if m.remaining(today) == 0)} out of daily allowance, "
            f"{sum(1 for m in pool if not m.can_send_to(recipient_domain, now))} in domain cool-off, "
            f"{sum(1 for m in pool if m.last_sent_at is not None and now - m.last_sent_at < MIN_GAP)} in pacing gap"
        )
    rng = rng or random
    weights = [m.remaining(today) for m in eligible]
    return rng.choices(eligible, weights=weights, k=1)[0]


def next_slot(mailbox: Mailbox, now: datetime, window: SendWindow,
              rng: random.Random | None = None) -> datetime:
    """Human-shaped pacing: a randomised gap, never a round number, always inside the
    window. Machine-regular timing is itself a spam signal."""
    rng = rng or random
    gap = timedelta(seconds=rng.randint(int(MIN_GAP.total_seconds()),
                                        int(MAX_GAP.total_seconds())))
    # The gap paces sends *relative to each other*. A mailbox that has not sent yet has
    # nothing to be paced against, so applying a gap there just delays every first send
    # - and on a cold start that deferred the whole queue on every tick.
    if mailbox.last_sent_at is None:
        return window.next_open(now)
    return window.next_open(max(now, mailbox.last_sent_at + gap))


def record_send(mailbox: Mailbox, recipient_domain: str, at: datetime) -> None:
    mailbox.sent_today += 1
    mailbox.last_sent_at = at
    mailbox.recent_domains[recipient_domain.lower()] = at


def roll_over_day(pool: list[Mailbox], today: date) -> None:
    """Called at local midnight: reset counters and advance warming mailboxes."""
    for m in pool:
        m.sent_today = 0
        if m.status is MailboxStatus.WARMING and m.ramp_started_on:
            if m.ramp_day(today) >= days_to_full_ramp(m.daily_cap):
                m.status = MailboxStatus.HEALTHY


def pool_capacity(pool: list[Mailbox], today: date) -> dict[str, int]:
    return {
        "mailboxes": len(pool),
        "sendable_today": sum(m.remaining(today) for m in pool),
        "at_full_ramp": sum(m.daily_cap for m in pool
                            if m.status is not MailboxStatus.DISABLED),
        "quarantined": sum(1 for m in pool if m.status is MailboxStatus.QUARANTINED),
    }
