import pytest

from coldstack.pipeline.build_list import build_list
from coldstack.pipeline.export import write_csv
from coldstack.providers import fake  # noqa: F401  registers adapters
from coldstack.providers.base import SearchQuery
from coldstack.providers.registry import email_finders, search_providers, verifiers


def _kit():
    return dict(
        search=search_providers()["fake"](), search_key="",
        finders={"fake": email_finders()["fake"]()}, finder_keys={"fake": "x"},
        verifier=verifiers()["fake"](), verifier_key=None,
    )


async def test_end_to_end_produces_verified_leads():
    leads, report = await build_list(SearchQuery(limit=50), **_kit())
    assert report.searched == 50
    assert report.emails_found > 0
    assert report.verified_valid > 0
    assert all(l.email for l in leads if l.email_status == "valid")


async def test_search_and_finder_from_one_vendor_do_not_collide_in_the_ledger():
    """Regression: FakeSearch and FakeFinder are both named 'fake'."""
    _, report = await build_list(SearchQuery(limit=20), **_kit())
    assert "search:fake" in report.per_provider
    assert report.per_provider["fake"]["attempts"] == report.after_suppression


async def test_suppression_runs_before_enrichment_spend():
    """Suppressed domains must cost nothing - not be filtered after we paid to enrich."""
    kit = _kit()
    baseline_leads, baseline = await build_list(SearchQuery(limit=60), **kit)
    dom = next(l.domain for l in baseline_leads if l.domain)

    _, suppressed = await build_list(SearchQuery(limit=60), **kit,
                                     suppressed_domains={dom})
    assert suppressed.after_suppression < baseline.after_suppression
    assert suppressed.per_provider["fake"]["attempts"] == suppressed.after_suppression
    assert suppressed.total_cost_usd < baseline.total_cost_usd


async def test_budget_is_a_hard_stop():
    _, report = await build_list(SearchQuery(limit=200), **_kit(),
                                 budget_usd=0.05, concurrency=1)
    assert report.budget_exhausted
    assert report.total_cost_usd <= 0.10          # allows one in-flight record to land


async def test_per_lead_cost_is_populated():
    leads, _ = await build_list(SearchQuery(limit=30), **_kit())
    assert any(l.cost_usd > 0 for l in leads)


async def test_deterministic_for_the_same_query():
    a, _ = await build_list(SearchQuery(limit=25, titles=["CSO"]), **_kit())
    b, _ = await build_list(SearchQuery(limit=25, titles=["CSO"]), **_kit())
    assert [l.email for l in a] == [l.email for l in b]


async def test_csv_only_valid_excludes_catch_all_by_default(tmp_path):
    leads, _ = await build_list(SearchQuery(limit=120), **_kit())
    out = tmp_path / "leads.csv"

    n_all = write_csv(leads, out)
    n_valid = write_csv(leads, out, only_valid=True)
    n_plus_catch_all = write_csv(leads, out, only_valid=True, include_catch_all=True)

    assert n_valid < n_plus_catch_all <= n_all
    assert n_all == sum(1 for l in leads if l.email)


async def test_csv_never_writes_rows_without_an_email(tmp_path):
    leads, _ = await build_list(SearchQuery(limit=80), **_kit())
    out = tmp_path / "leads.csv"
    n = write_csv(leads, out)
    assert n == sum(1 for l in leads if l.email)
    assert "" not in [r.split(",")[12] for r in out.read_text().splitlines()[1:]]


async def test_csv_money_columns_have_no_float_noise(tmp_path):
    """0.0045000000000000005 in a spreadsheet reads as a bug, not a price."""
    leads, _ = await build_list(SearchQuery(limit=40), **_kit())
    out = tmp_path / "leads.csv"
    write_csv(leads, out)
    costs = [r.split(",")[-1] for r in out.read_text().splitlines()[1:]]
    assert costs, "expected rows"
    assert all(len(c.split(".")[1]) == 5 for c in costs), costs[:3]
