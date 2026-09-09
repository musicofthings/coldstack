from datetime import datetime, timedelta, timezone

import pytest

from coldstack.pipeline.build_list import build_list
from coldstack.providers import fake  # noqa: F401
from coldstack.providers.base import ProviderCost, SearchQuery, VerifyResult
from coldstack.providers.registry import email_finders, search_providers, verifiers
from coldstack.verify.cache import (InMemoryVerificationCache, is_fresh, ttl_for,
                                    verify_cached)


class CountingVerifier:
    name = "counting"
    est_unit_cost_usd = 0.005

    def __init__(self, verdict="valid"):
        self.calls = 0
        self.verdict = verdict

    async def verify(self, email, *, key=None):
        self.calls += 1
        return VerifyResult(email=email, verdict=self.verdict,
                            cost=ProviderCost(1, self.est_unit_cost_usd, hit=True))


async def test_second_lookup_is_free():
    cache, v = InMemoryVerificationCache(), CountingVerifier()
    a = await verify_cached("a@example.com", v, cache)
    b = await verify_cached("a@example.com", v, cache)
    assert v.calls == 1
    assert a.verdict == b.verdict == "valid"
    assert b.cost.unit_cost_usd == 0.0        # a reused verdict must not be billed


async def test_cache_is_case_insensitive():
    cache, v = InMemoryVerificationCache(), CountingVerifier()
    await verify_cached("Ana@Example.com", v, cache)
    await verify_cached("ana@example.com", v, cache)
    assert v.calls == 1


async def test_unknown_is_never_cached():
    """One transient failure must not make an address permanently unverifiable."""
    cache, v = InMemoryVerificationCache(), CountingVerifier(verdict="unknown")
    await verify_cached("x@example.com", v, cache)
    await verify_cached("x@example.com", v, cache)
    assert v.calls == 2


@pytest.mark.parametrize("verdict,days,expected", [
    ("valid", 30, True), ("valid", 90, False),
    ("catch_all", 5, True), ("catch_all", 30, False),
    ("invalid", 200, True),
    ("unknown", 0, False),
])
def test_ttls_differ_by_verdict(verdict, days, expected):
    checked = datetime.now(timezone.utc) - timedelta(days=days)
    assert is_fresh(verdict, checked) is expected


def test_catch_all_expires_sooner_than_valid():
    """Catch-all is the verdict most worth re-checking - it is what blocks a send."""
    assert ttl_for("catch_all") < ttl_for("valid") < ttl_for("invalid")


async def test_pipeline_reports_reused_verdicts():
    kit = dict(search=search_providers()["fake"](), search_key="",
               finders={"fake": email_finders()["fake"]()}, finder_keys={"fake": "x"},
               verifier=verifiers()["fake"](), verifier_key=None)
    cache = InMemoryVerificationCache()
    _, first = await build_list(SearchQuery(limit=60), **kit, verification_cache=cache)
    _, second = await build_list(SearchQuery(limit=60), **kit, verification_cache=cache)

    assert first.verifications_reused == 0
    assert second.verifications_reused > 0
    verify_cost = lambda r: sum(v["cost"] for k, v in r.per_provider.items()
                                if k.startswith("verify:"))
    assert verify_cost(second) < verify_cost(first)
