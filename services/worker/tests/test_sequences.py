from datetime import datetime, timedelta, timezone

import pytest

from coldstack.sending.sequences import (Enrollment, EnrollmentStatus, Outcome,
                                         Sequence, Step, Variant, advance, decide,
                                         due_enrollments, on_bounce, on_reply,
                                         on_unsubscribe)

NOW = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)


def seq(n=3, variants=1):
    return Sequence([
        Step(index=i,
             variants=[Variant(id=f"s{i}v{v}", label=chr(65 + v),
                               subject=f"subject {i}{v}", body=f"body {i}{v}")
                       for v in range(variants)],
             delay_days=3)
        for i in range(n)])


def enr(**kw):
    return Enrollment(**{"id": "e1", "contact_email": "a@acme.com", **kw})


def d(e, s=None, **kw):
    return decide(e, s or seq(), NOW,
                  suppressed_emails=kw.get("emails", set()),
                  suppressed_domains=kw.get("domains", set()))


# ------------------------------------------------------------- variants

def test_variant_assignment_is_stable_across_repeated_calls():
    """A retry must not re-roll the contact into the other arm - that silently
    contaminates every A/B result."""
    s = seq(variants=2)
    picks = {s.step(0).variant_for("enrollment-abc").id for _ in range(50)}
    assert len(picks) == 1


def test_variants_split_roughly_by_weight():
    s = Sequence([Step(0, [Variant("a", "A", "s", "b", weight=3.0),
                           Variant("b", "B", "s", "b", weight=1.0)])])
    picks = [s.step(0).variant_for(f"e{i}").id for i in range(2000)]
    ratio = picks.count("a") / len(picks)
    assert 0.70 < ratio < 0.80


# ------------------------------------------------------------- gating

def test_suppression_is_checked_at_send_time():
    """Enrolled last week, unsubscribed yesterday: the next send must not go."""
    e = enr()
    assert d(e, emails={"a@acme.com"}).outcome is Outcome.SKIP_SUPPRESSED
    assert e.status is EnrollmentStatus.SUPPRESSED


def test_domain_level_suppression_blocks_every_address_there():
    assert d(enr(), domains={"acme.com"}).outcome is Outcome.SKIP_SUPPRESSED


def test_not_due_yet_is_skipped():
    e = enr(next_send_at=NOW + timedelta(days=1))
    assert d(e).outcome is Outcome.SKIP_NOT_DUE


def test_replied_enrollment_never_sends_again():
    assert d(enr(status=EnrollmentStatus.REPLIED)).outcome is Outcome.SKIP_INACTIVE


def test_running_past_the_last_step_completes():
    e = enr(current_step=3)
    assert d(e).outcome is Outcome.COMPLETE
    assert e.status is EnrollmentStatus.COMPLETED


# ------------------------------------------------------------- advancing

def test_advance_schedules_the_next_step_and_threads():
    s, e = seq(), enr()
    advance(e, s, NOW, "<msg-1@x>")
    assert e.current_step == 1
    assert e.next_send_at == NOW + timedelta(days=3)
    assert e.thread_id == "<msg-1@x>"
    assert e.references == ["<msg-1@x>"]


def test_advance_past_the_final_step_completes_and_clears_the_schedule():
    s, e = seq(n=2), enr(current_step=1)
    advance(e, s, NOW, "<m@x>")
    assert e.status is EnrollmentStatus.COMPLETED
    assert e.next_send_at is None


def test_references_accumulate_for_threading():
    s, e = seq(), enr()
    advance(e, s, NOW, "<m1@x>")
    advance(e, s, NOW + timedelta(days=3), "<m2@x>")
    assert e.references == ["<m1@x>", "<m2@x>"]


# ------------------------------------------------------------- events

def test_a_reply_stops_every_campaign_for_that_person():
    """Answering campaign A then receiving step 3 of campaign B is the worst outcome
    in the whole system - it tells the prospect exactly what you are."""
    a = Enrollment(id="a", contact_email="x@acme.com")
    b = Enrollment(id="b", contact_email="X@ACME.COM")
    c = Enrollment(id="c", contact_email="other@acme.com")
    stopped = on_reply([a, b, c], "x@acme.com")
    assert {e.id for e in stopped} == {"a", "b"}
    assert c.is_active


def test_hard_bounce_suppresses_the_address():
    e = enr()
    assert on_bounce(e) == "a@acme.com"
    assert e.status is EnrollmentStatus.BOUNCED


def test_soft_bounce_does_not_suppress():
    e = enr()
    assert on_bounce(e, hard=False) is None
    assert e.is_active


def test_unsubscribe_stops_all_and_returns_the_address():
    a, b = Enrollment(id="a", contact_email="x@acme.com"), enr()
    assert on_unsubscribe([a, b], "x@acme.com") == "x@acme.com"
    assert a.status is EnrollmentStatus.UNSUBSCRIBED and b.is_active


def test_due_list_excludes_inactive_and_future():
    active_now = Enrollment(id="1", contact_email="a@x.com")
    future = Enrollment(id="2", contact_email="b@x.com", next_send_at=NOW + timedelta(days=1))
    replied = Enrollment(id="3", contact_email="c@x.com", status=EnrollmentStatus.REPLIED)
    assert [e.id for e in due_enrollments([active_now, future, replied], NOW)] == ["1"]
