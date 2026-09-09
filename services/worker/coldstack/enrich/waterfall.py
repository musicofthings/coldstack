"""Cost-aware email-finding waterfall.

The single biggest cost lever in the whole product. Instead of paying one vendor's
blended rate for every lead, try providers cheapest-first and stop on the first hit
that verifies. Ordering is not static: it is re-derived from the workspace's own
observed cost-per-hit, because hit rates are ICP-dependent (a vendor that is great
for US SaaS may be useless for Indian manufacturing).

    effective_cost_per_hit = unit_cost / max(hit_rate, floor)

Providers with too few observations fall back to their list price and a prior hit
rate, and are occasionally explored anyway so a good cheap provider is not starved.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from ..providers.base import EmailFinder, PersonRec, ProviderCost

MIN_OBSERVATIONS = 50
HIT_RATE_FLOOR = 0.05
EXPLORE_PROB = 0.10


@dataclass(slots=True)
class ProviderStats:
    attempts: int = 0
    hits: int = 0
    unit_cost_usd: float = 0.0

    @property
    def hit_rate(self) -> float:
        return self.hits / self.attempts if self.attempts else 0.35  # prior

    @property
    def cost_per_hit(self) -> float:
        return self.unit_cost_usd / max(self.hit_rate, HIT_RATE_FLOOR)


def order_providers(stats: dict[str, ProviderStats]) -> list[str]:
    ranked = sorted(stats, key=lambda n: stats[n].cost_per_hit)
    if len(ranked) > 1 and random.random() < EXPLORE_PROB:
        under = [n for n in ranked if stats[n].attempts < MIN_OBSERVATIONS]
        if under:
            pick = random.choice(under)
            ranked.remove(pick)
            ranked.insert(0, pick)
    return ranked


async def find_email(
    person: PersonRec,
    finders: dict[str, EmailFinder],
    keys: dict[str, str],
    stats: dict[str, ProviderStats],
    budget_usd: float | None = None,
) -> tuple[str | None, list[tuple[str, ProviderCost]]]:
    """Returns (email_or_None, [(provider, cost), ...]) — the trail is written to usage_ledger."""
    spent, trail = 0.0, []
    for name in order_providers(stats):
        finder, key = finders.get(name), keys.get(name)
        if not finder or not key:
            continue
        if budget_usd is not None and spent + finder.est_unit_cost_usd > budget_usd:
            break
        email, cost = await finder.find_email(person, key=key)
        spent += cost.unit_cost_usd * cost.units
        trail.append((name, cost))
        if email:
            return email, trail
    return None, trail
