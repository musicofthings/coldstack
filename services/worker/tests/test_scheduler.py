import random
from datetime import date, datetime, timedelta, timezone

import pytest

from coldstack.sending.scheduler import (DOMAIN_COOLOFF, Mailbox, MailboxStatus,
                                         NoMailboxAvailable, SendWindow,
                                         days_to_full_ramp, next_slot, pick_mailbox,
                                         pool_capacity, ramp_cap, record_send,
                                         roll_over_day)

TODAY = date(2026, 9, 9)
NOW = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)      # a Wednesday, mid-window


def mb(**kw):
    base = dict(id="m1", email="a@example.com", daily_cap=40,
                ramp_started_on=TODAY - timedelta(days=30), status=MailboxStatus.HEALTHY)
    return Mailbox(**{**base, **kw})


# ---------------------------------------------------------------- ramp

def test_ramp_starts_at_five():
    assert ramp_cap(0, 40) == 5


def test_ramp_reaches_cap_in_the_documented_two_to_three_weeks():
    """Regression: a 1.3 growth factor hit the cap in 8 days, roughly twice as fast as
    the policy this module exists to enforce."""
    assert 14 <= days_to_full_ramp(40) <= 21


def test_ramp_is_monotonic_and_never_exceeds_the_cap():
    caps = [ramp_cap(d, 40) for d in range(0, 40)]
    assert caps == sorted(caps)
    assert max(caps) == 40


def test_a_brand_new_mailbox_cannot_send_five_hundred():
    m = mb(ramp_started_on=TODAY - timedelta(days=2), daily_cap=500)
    assert m.allowance(TODAY) < 10


def test_quarantined_mailbox_has_no_allowance():
    assert mb(status=MailboxStatus.QUARANTINED).allowance(TODAY) == 0


def test_throttled_mailbox_is_halved():
    assert mb(status=MailboxStatus.THROTTLED).allowance(TODAY) == 20


# ---------------------------------------------------------------- rotation

def test_picks_only_mailboxes_with_allowance_left():
    spent, fresh = mb(id="spent", sent_today=40), mb(id="fresh")
    assert pick_mailbox([spent, fresh], "acme.com", NOW).id == "fresh"


def test_domain_cooloff_blocks_a_second_hit_from_the_same_mailbox():
    m = mb()
    record_send(m, "acme.com", NOW)
    with pytest.raises(NoMailboxAvailable):
        pick_mailbox([m], "acme.com", NOW + timedelta(hours=1))
    # a different domain is fine immediately
    assert pick_mailbox([m], "other.com", NOW + timedelta(hours=1)).id == m.id
    # and the same domain is fine once the cool-off has passed
    assert pick_mailbox([m], "acme.com", NOW + DOMAIN_COOLOFF).id == m.id


def test_exhausted_pool_explains_why():
    m = mb(sent_today=40)
    with pytest.raises(NoMailboxAvailable, match="out of daily allowance"):
        pick_mailbox([m], "acme.com", NOW)


def test_load_is_weighted_towards_warmer_mailboxes():
    warm, cold = mb(id="warm", daily_cap=40), mb(
        id="cold", daily_cap=40, ramp_started_on=TODAY - timedelta(days=1))
    rng = random.Random(7)
    picks = [pick_mailbox([warm, cold], f"d{i}.com", NOW, rng).id for i in range(400)]
    assert picks.count("warm") > picks.count("cold") * 3


# ---------------------------------------------------------------- pacing

def test_sends_are_spaced_and_never_a_round_number():
    m, rng = mb(), random.Random(3)
    m.last_sent_at = NOW
    gaps = set()
    for _ in range(30):
        slot = next_slot(m, NOW, SendWindow(0, 24, tuple(range(7))), rng)
        gaps.add((slot - NOW).total_seconds())
    assert len(gaps) > 20                       # not a fixed cadence
    assert all(90 <= g <= 600 for g in gaps)


def test_window_pushes_out_of_hours_sends_to_the_next_morning():
    w = SendWindow(9, 17, (0, 1, 2, 3, 4))
    evening = datetime(2026, 9, 9, 22, 0, tzinfo=timezone.utc)
    assert w.next_open(evening).hour == 9
    assert w.next_open(evening).day == 10


def test_window_skips_the_weekend():
    w = SendWindow(9, 17, (0, 1, 2, 3, 4))
    friday_night = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)
    assert w.next_open(friday_night).weekday() == 0      # Monday


# ---------------------------------------------------------------- day roll

def test_day_roll_resets_counters_and_graduates_warm_mailboxes():
    warming = mb(id="w", status=MailboxStatus.WARMING, sent_today=12,
                 ramp_started_on=TODAY - timedelta(days=30))
    roll_over_day([warming], TODAY)
    assert warming.sent_today == 0
    assert warming.status is MailboxStatus.HEALTHY


def test_capacity_report():
    pool = [mb(id="a"), mb(id="b", status=MailboxStatus.QUARANTINED)]
    cap = pool_capacity(pool, TODAY)
    assert cap["mailboxes"] == 2 and cap["quarantined"] == 1
    assert cap["sendable_today"] == 40
