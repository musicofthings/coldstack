"""Derived signals.

ListKit sells "21 billion daily intent signals" and its own reviewers call the result
hit-or-miss. The alternative is not a better black box - it is signals a human can
audit. Every signal here carries a plain-English summary, a score, an observed date and
**a source URL**, so a salesperson can click it before quoting it, and so the
personalisation gate has something to verify against.

Scoring is intentionally crude and documented rather than learned. A recency-weighted
score you can read beats a model output nobody can question.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(slots=True)
class SignalRecord:
    kind: str                 # hiring | tech_change | funding | job_change | github | news
    summary: str
    observed_at: datetime
    source_url: str | None = None
    score: float = 0.0
    company_domain: str | None = None
    raw: dict = field(default_factory=dict)

    def age_days(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        observed = self.observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return max(0.0, (now - observed).total_seconds() / 86400)


def recency_weight(age_days: float, half_life_days: float = 30.0) -> float:
    """Halves every `half_life_days`. A hire posted last week is worth far more than
    the same hire six months ago, and a signal with no decay is just a fact."""
    return 0.5 ** (age_days / half_life_days)


def score_signal(base: float, record: SignalRecord, *, half_life_days: float = 30.0,
                 now: datetime | None = None) -> float:
    return round(base * recency_weight(record.age_days(now), half_life_days), 3)


def to_personalisation_signals(records: list[SignalRecord]):
    """Convert to the citation-gated personaliser's input, highest score first."""
    from ..ai.personalize import Signal
    ranked = sorted(records, key=lambda r: r.score, reverse=True)
    return [Signal(id=i + 1, summary=r.summary, source_url=r.source_url, kind=r.kind)
            for i, r in enumerate(ranked)]
