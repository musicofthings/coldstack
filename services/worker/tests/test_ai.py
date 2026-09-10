"""The personalisation gate is the centrepiece: an invented compliment is worse than
no personalisation at all, so it must be structurally impossible rather than discouraged."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from coldstack.ai.copy import Severity, generate, lint
from coldstack.ai.icp import validate
from coldstack.ai.llm import FakeLlm, LlmResult
from coldstack.ai.personalize import Signal, personalise, verify_line
from coldstack.signals.base import (SignalRecord, recency_weight, score_signal,
                                    to_personalisation_signals)

NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)
LEAD = {"full_name": "Ana Cruz", "title": "Head of Laboratory", "company": "Kestrel Bio"}
SIGNALS = [
    Signal(id=1, summary="Kestrel Bio posted 3 lab technician roles in August",
           source_url="https://example.com/jobs", kind="hiring"),
    Signal(id=2, summary="Kestrel Bio pushed to 9 repositories in the last 90 days",
           source_url="https://github.com/orgs/kestrel/repositories", kind="github"),
]


def _resp(lines):
    return json.dumps({"lines": lines})


# ---------------------------------------------------------------- the gate

async def test_grounded_line_is_kept():
    llm = FakeLlm([_resp([{"text": "Saw Kestrel Bio posted 3 lab technician roles in August.",
                           "cites": [1]}])])
    res = await personalise(LEAD, SIGNALS, llm)
    assert res.text and "3 lab technician roles" in res.text
    assert res.sources == ["https://example.com/jobs"]
    assert res.used_fallback is False


async def test_an_invented_name_is_rejected():
    """The classic failure: a plausible product that does not exist."""
    llm = FakeLlm([_resp([{"text": "Loved your work on the Helios sequencing platform.",
                           "cites": [1]}])])
    res = await personalise(LEAD, SIGNALS, llm, fallback="Quick one for you.")
    assert res.text == "Quick one for you."
    assert res.used_fallback is True
    assert any("Helios" in why for _, why in res.rejected)


async def test_an_invented_number_is_rejected():
    llm = FakeLlm([_resp([{"text": "Kestrel Bio posted 47 roles in August.", "cites": [1]}])])
    res = await personalise(LEAD, SIGNALS, llm)
    assert res.used_fallback is True
    assert any("47" in why for _, why in res.rejected)


async def test_a_line_citing_nothing_is_rejected():
    llm = FakeLlm([_resp([{"text": "Your lab looks like it is scaling fast.", "cites": []}])])
    res = await personalise(LEAD, SIGNALS, llm)
    assert res.used_fallback is True
    assert any("cites no supplied signal" in why for _, why in res.rejected)


async def test_a_line_citing_an_unknown_signal_id_is_rejected():
    llm = FakeLlm([_resp([{"text": "Kestrel Bio raised a Series B.", "cites": [99]}])])
    res = await personalise(LEAD, SIGNALS, llm)
    assert res.used_fallback is True


async def test_no_signals_means_the_model_is_never_asked():
    """Nothing true to say is a correct outcome, not a prompt to be creative."""
    llm = FakeLlm([_resp([{"text": "should never be generated", "cites": [1]}])])
    res = await personalise(LEAD, [], llm, fallback="Hi there.")
    assert res.text == "Hi there." and res.used_fallback
    assert llm.prompts == []


async def test_llm_failure_falls_back_rather_than_raising():
    res = await personalise(LEAD, SIGNALS, FakeLlm(ok=False), fallback="Hello.")
    assert res.text == "Hello." and res.used_fallback


async def test_mixed_output_keeps_only_the_verified_line():
    llm = FakeLlm([_resp([
        {"text": "Kestrel Bio posted 3 lab technician roles in August.", "cites": [1]},
        {"text": "Also loved your Helios launch.", "cites": [1]},
    ])])
    res = await personalise(LEAD, SIGNALS, llm)
    assert "3 lab technician roles" in res.text
    assert "Helios" not in res.text
    assert len(res.rejected) == 1


def test_the_lead_own_details_are_allowed_without_a_signal_mentioning_them():
    ok, why = verify_line("Ana, saw Kestrel Bio posted 3 lab technician roles in August.",
                          [1], {s.id: s for s in SIGNALS},
                          {"Ana", "Cruz", "Kestrel", "Bio", "Kestrel Bio"})
    assert ok, why


def test_an_over_long_line_is_rejected():
    long_line = "Kestrel Bio posted 3 lab technician roles in August " + "and more " * 20
    ok, why = verify_line(long_line, [1], {s.id: s for s in SIGNALS}, set())
    assert not ok and "words" in why


# ---------------------------------------------------------------- ICP validation

def test_unknown_filters_are_dropped_and_reported_loudly():
    """Silently ignoring a filter is the dangerous case: the user believes they
    targeted Series-A companies and pays to enrich everybody."""
    t = validate({"titles": ["CTO"], "funding_stage": "series_a", "uses_competitor": True})
    assert t.query.titles == ["CTO"]
    assert set(t.dropped) == {"funding_stage", "uses_competitor"}
    assert any("NOT applied" in w for w in t.warnings)


def test_bad_seniority_values_are_filtered_not_passed_through():
    t = validate({"seniorities": ["c_suite", "Supreme Overlord", "VP"]})
    assert t.query.seniorities == ["c_suite", "vp"]
    assert any("unsupported seniority" in w for w in t.warnings)


def test_a_string_is_accepted_where_a_list_is_expected():
    assert validate({"countries": "India"}).query.countries == ["India"]


def test_reversed_headcount_bounds_are_swapped():
    t = validate({"headcount_min": 500, "headcount_max": 20, "titles": ["CEO"]})
    assert (t.query.headcount_min, t.query.headcount_max) == (20, 500)
    assert any("swapped" in w for w in t.warnings)


def test_a_query_with_no_filters_warns_that_it_matches_everyone():
    t = validate({"countries": ["India"]})
    assert any("almost anyone" in w for w in t.warnings)


def test_non_numeric_headcount_is_dropped():
    t = validate({"headcount_min": "twenty", "titles": ["CEO"]})
    assert t.query.headcount_min is None and "headcount_min" in t.dropped


# ---------------------------------------------------------------- copy lint

def test_clean_copy_passes():
    r = lint("quick question about your lab", "Saw you're hiring technicians. Worth a chat?")
    assert r.ok


def test_exclamation_in_subject_fails():
    r = lint("Amazing offer!", "Body text here.")
    assert not r.ok
    assert any(f.check == "subject punctuation" for f in r.findings)


def test_link_in_the_first_touch_fails():
    r = lint("hi", "Have a look: https://example.com/demo", step_index=0)
    assert not r.ok
    assert any(f.check == "link in first touch" for f in r.findings)


def test_the_same_link_is_only_a_warning_on_a_later_step():
    r = lint("", "Have a look: https://example.com/demo", step_index=2)
    assert r.ok


def test_spam_phrases_fail():
    assert not lint("hi", "Act now, this is a limited time offer.").ok


def test_cliches_warn_but_do_not_fail():
    r = lint("hi", "I hope this email finds you well. Worth a chat?")
    assert r.ok
    assert any(f.check == "cliché" for f in r.findings)


def test_title_case_subject_warns():
    r = lint("Quick Question About Your Lab", "Body.")
    assert any(f.check == "subject case" for f in r.findings)


def test_unresolved_tokens_are_flagged_for_review():
    r = lint("hi {{first_name}}", "Hello {{company}}.")
    f = next(f for f in r.findings if f.check == "tokens")
    assert f.severity is Severity.INFO
    assert "company" in f.detail and "first_name" in f.detail


async def test_generation_lints_every_variant_it_produces():
    body = {"steps": [{"delay_days": 0, "variants": [
        {"label": "A", "subject": "quick question", "body": "Saw you're hiring. Worth a chat?"},
        {"label": "B", "subject": "Act Now!", "body": "Click here: https://x.com"}]}]}
    g = await generate("offer", "audience", FakeLlm([json.dumps(body)]))
    assert g.ok and g.sequence is not None
    assert g.has_failures                          # variant B must be flagged
    bad = [r for i, label, r in g.lint_reports if label == "B"][0]
    assert not bad.ok


async def test_generation_failure_degrades_without_raising():
    g = await generate("offer", "audience", FakeLlm(ok=False))
    assert g.ok is False and g.sequence is None


async def test_prose_wrapped_json_is_still_parsed():
    body = ('Sure! Here is your sequence:\n```json\n'
            '{"steps":[{"delay_days":0,"variants":[{"label":"A","subject":"hi","body":"Worth a chat?"}]}]}'
            '\n```\nLet me know!')
    g = await generate("o", "a", FakeLlm([body]))
    assert g.ok and g.sequence and len(g.sequence.steps) == 1


# ---------------------------------------------------------------- signal scoring

def test_recency_halves_at_the_half_life():
    assert recency_weight(30, 30) == pytest.approx(0.5)
    assert recency_weight(60, 30) == pytest.approx(0.25)


def test_a_fresh_hire_outscores_an_old_one():
    fresh = SignalRecord("hiring", "posted 3 roles", NOW - timedelta(days=3))
    stale = SignalRecord("hiring", "posted 3 roles", NOW - timedelta(days=200))
    assert score_signal(1.0, fresh, now=NOW) > score_signal(1.0, stale, now=NOW) * 5


def test_conversion_to_personalisation_signals_is_ranked_and_numbered():
    a = SignalRecord("hiring", "low", NOW, score=0.1)
    b = SignalRecord("github", "high", NOW, score=0.9)
    out = to_personalisation_signals([a, b])
    assert [s.id for s in out] == [1, 2]
    assert out[0].summary == "high"
