from datetime import date, datetime, timedelta, timezone

import pytest

from coldstack.sending.base import SendResult
from coldstack.sending.breakers import SendStats
from coldstack.sending.policy import CampaignClass, PolicyViolation
from coldstack.sending.registry import all_transports
from coldstack.sending.scheduler import Mailbox, MailboxStatus, SendWindow
from coldstack.sending.sequences import Enrollment, EnrollmentStatus, Sequence, Step, Variant
from coldstack.sending.warmup import WARMUP_HEADER, is_warmup
from coldstack.sending.worker import CampaignContext, tick

NOW = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)      # Wednesday, mid-window
TODAY = NOW.date()
ANY_TIME = SendWindow(0, 24, tuple(range(7)))


class SpyTransport:
    name = "spy"

    def __init__(self, ok=True):
        from coldstack.sending.transports.mailbox import MAILBOX_CAPS
        self.caps = MAILBOX_CAPS
        self.ok = ok
        self.sent = []

    async def send(self, req, *, creds):
        self.sent.append(req)
        if not self.ok:
            return SendResult(ok=False, error="550 rejected", retryable=False)
        return SendResult(ok=True, message_id_hdr=f"<m{len(self.sent)}@x>")

    async def send_batch(self, reqs, *, creds):
        return [await self.send(r, creds=creds) for r in reqs]

    async def test(self, creds):
        return True, "ok"


def ctx(**kw):
    base = dict(
        campaign_id="c1", campaign_class=CampaignClass.COLD,
        sequence=Sequence([Step(0, [Variant("v0", "A", "Hi {{first_name}}", "Hello {{first_name}}")]),
                           Step(1, [Variant("v1", "A", None, "Following up")], delay_days=3)]),
        mailboxes=[Mailbox(id="m1", email="me@sender.com",
                           ramp_started_on=TODAY - timedelta(days=60),
                           status=MailboxStatus.HEALTHY)],
        transport=SpyTransport(), creds={}, window=ANY_TIME)
    return CampaignContext(**{**base, **kw})


def pool(n):
    """n independent mailboxes. One mailbox can only send once per instant - that is
    what the pacing gap is for - so throughput within a single tick is bounded by pool
    size, not by queue length."""
    return [Mailbox(id=f"m{i}", email=f"me{i}@sender{i}.com",
                    ramp_started_on=TODAY - timedelta(days=60),
                    status=MailboxStatus.HEALTHY) for i in range(n)]


def enrollments(n=3):
    return [Enrollment(id=f"e{i}", contact_email=f"p{i}@target{i}.com") for i in range(n)]


async def test_a_tick_sends_and_advances():
    c, es = ctx(mailboxes=pool(3)), enrollments(3)
    r = await tick(NOW, es, c)
    assert r.sent == 3
    assert all(e.current_step == 1 for e in es)
    assert all(e.next_send_at == NOW + timedelta(days=3) for e in es)


async def test_one_mailbox_does_not_fire_a_burst_in_a_single_instant():
    """Regression on the pacing rule: a single mailbox sends once per instant. The rest
    stay due (next_send_at is left None) so the following tick retries them as soon as
    the gap has elapsed - rather than guessing a future time when capacity will free."""
    c, es = ctx(mailboxes=pool(1)), enrollments(4)
    r = await tick(NOW, es, c)
    assert r.sent == 1
    assert r.deferred == 3
    waiting = [e for e in es if e.current_step == 0]
    assert len(waiting) == 3
    assert all(e.next_send_at is None and e.is_active for e in waiting)
    assert any("pacing gap" in n for n in r.notes)


async def test_a_deferred_enrollment_sends_on_a_later_tick():
    c, es = ctx(mailboxes=pool(1)), enrollments(2)
    first = await tick(NOW, es, c)
    later = await tick(NOW + timedelta(minutes=20), es, c)
    assert first.sent == 1 and later.sent == 1
    assert all(e.current_step == 1 for e in es)


async def test_personalisation_is_rendered():
    c, es = ctx(mailboxes=pool(1)), enrollments(1)
    await tick(NOW, es, c, {"p0@target0.com": {"first_name": "Ana", "full_name": "Ana Cruz"}})
    assert c.transport.sent[0].subject == "Hi Ana"
    assert c.transport.sent[0].to_name == "Ana Cruz"


def test_unknown_token_is_left_visible_not_blanked():
    from coldstack.sending.worker import _render
    assert _render("Hi {{first_name}}", {}) == "Hi {{first_name}}"


async def test_every_send_carries_one_click_unsubscribe():
    c, es = ctx(mailboxes=pool(1)), enrollments(1)
    await tick(NOW, es, c)
    h = c.transport.sent[0].headers
    assert "List-Unsubscribe" in h and "One-Click" in h["List-Unsubscribe-Post"]


async def test_second_step_threads_onto_the_first():
    c, es = ctx(mailboxes=pool(1)), enrollments(1)
    await tick(NOW, es, c)
    later = NOW + timedelta(days=3)
    await tick(later, es, c)
    assert c.transport.sent[1].in_reply_to == "<m1@x>"
    assert c.transport.sent[1].references == ["<m1@x>"]


async def test_idempotency_key_is_stable_per_step():
    c, es = ctx(mailboxes=pool(1)), enrollments(1)
    await tick(NOW, es, c)
    assert c.transport.sent[0].idempotency_key == "c1:e0:0"


async def test_suppressed_contact_never_consumes_mailbox_allowance():
    c = ctx(mailboxes=pool(3), suppressed_domains={"target1.com"})
    es = enrollments(3)
    r = await tick(NOW, es, c)
    assert r.suppressed == 1 and r.sent == 2
    assert sum(m.sent_today for m in c.mailboxes) == 2


async def test_daily_cap_defers_rather_than_overshooting():
    """Two mailboxes capped at 1/day: the third contact cannot be sent today at all,
    and the tick says why instead of silently dropping it."""
    boxes = pool(2)
    for m in boxes:
        m.daily_cap = 1
        m.ramp_started_on = TODAY - timedelta(days=60)
    c = ctx(mailboxes=boxes)
    es = enrollments(3)
    r = await tick(NOW, es, c)
    assert r.sent == 2
    assert r.deferred == 1
    assert any("daily allowance" in n for n in r.notes)
    assert sum(m.sent_today for m in boxes) == 2


async def test_breaker_pauses_the_tick_before_anything_is_sent():
    c = ctx(stats=SendStats(sent=300, hard_bounces=15, replies=1))
    r = await tick(NOW, enrollments(3), c)
    assert r.paused and r.sent == 0
    assert c.transport.sent == []


async def test_auth_failure_pauses_even_with_a_tiny_sample():
    c = ctx(stats=SendStats(sent=2, auth_failures=1))
    assert (await tick(NOW, enrollments(1), c)).paused


async def test_cold_campaign_refuses_an_esp_transport():
    c = ctx(transport=all_transports()["resend"])
    with pytest.raises(PolicyViolation):
        await tick(NOW, enrollments(1), c)


async def test_hard_rejection_counts_as_a_bounce():
    c = ctx(mailboxes=pool(2), transport=SpyTransport(ok=False))
    r = await tick(NOW, enrollments(2), c)
    assert r.failed == 2 and r.sent == 0
    assert c.stats.hard_bounces == 2


async def test_outside_the_send_window_nothing_sends_and_it_is_rescheduled():
    c = ctx(window=SendWindow(9, 17, (0, 1, 2, 3, 4)))
    saturday = datetime(2026, 9, 12, 11, 0, tzinfo=timezone.utc)
    es = enrollments(2)
    r = await tick(saturday, es, c)
    assert r.sent == 0 and r.deferred == 2
    assert all(e.next_send_at.weekday() == 0 for e in es)      # pushed to Monday


async def test_dry_run_touches_no_transport_but_still_advances():
    c, es = ctx(mailboxes=pool(2)), enrollments(2)
    r = await tick(NOW, es, c, dry_run=True)
    assert r.sent == 2 and c.transport.sent == []
    assert all(e.current_step == 1 for e in es)


def test_warmup_traffic_is_identifiable_so_analytics_can_exclude_it():
    assert is_warmup({WARMUP_HEADER: "1"})
    assert is_warmup({"x-coldstack-warmup": "1"})     # header names are case-insensitive
    assert not is_warmup({"Subject": "hi"})
