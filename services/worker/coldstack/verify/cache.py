"""Verification cache.

Verification is the second-largest line item after data, and a verdict does not change
minute to minute - so re-buying one you already own is pure waste. Every verdict is
cached with a TTL and re-checked only once it goes stale.

TTLs differ by verdict on purpose, because their shelf lives differ:

  invalid     long  - a mailbox that does not exist rarely starts existing
  valid       ~60d  - people leave jobs; a stale "valid" is how you bounce
  catch_all   short - domain policy changes, and catch-all is the verdict most worth
                      re-checking because it is the one blocking a send
  risky       short - same reasoning
  unknown     ~0    - a failed check is not a result; never cache it as one

That last line matters: caching "unknown" would turn one transient network blip into a
permanently unverifiable address.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from ..providers.base import VerifyResult

TTL_DAYS: dict[str, int] = {
    "invalid": 365,
    "valid": 60,
    "catch_all": 14,
    "risky": 14,
    "unknown": 0,
}


def ttl_for(verdict: str) -> timedelta:
    return timedelta(days=TTL_DAYS.get(verdict, 0))


def is_fresh(verdict: str, checked_at: datetime, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    return now - checked_at < ttl_for(verdict)


@dataclass(slots=True)
class CachedVerdict:
    result: VerifyResult
    checked_at: datetime


class VerificationCache(Protocol):
    def get(self, email: str) -> CachedVerdict | None: ...
    def put(self, email: str, result: VerifyResult) -> None: ...


@dataclass
class InMemoryVerificationCache:
    _rows: dict[str, CachedVerdict] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0
    stale: int = 0

    def get(self, email: str) -> CachedVerdict | None:
        row = self._rows.get(email.lower())
        if row is None:
            self.misses += 1
            return None
        if not is_fresh(row.result.verdict, row.checked_at):
            self.stale += 1
            return None
        self.hits += 1
        return row

    def put(self, email: str, result: VerifyResult) -> None:
        if result.verdict == "unknown":
            return                      # a failed check is not a result
        self._rows[email.lower()] = CachedVerdict(result, datetime.now(timezone.utc))

    @property
    def saved_calls(self) -> int:
        return self.hits


async def verify_cached(email: str, verifier, cache: VerificationCache,
                        *, key: str | None = None) -> VerifyResult:
    """Wrap any Verifier with the cache. Cached results report zero cost, which keeps
    the ledger honest about what a run actually spent."""
    hit = cache.get(email)
    if hit is not None:
        cached = hit.result
        cached.cost.units = 0
        cached.cost.unit_cost_usd = 0.0
        return cached
    result = await verifier.verify(email, key=key)
    cache.put(email, result)
    return result
