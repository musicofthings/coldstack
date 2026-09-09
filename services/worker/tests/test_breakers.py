import pytest

from coldstack.sending.breakers import Action, SendStats, evaluate


def test_small_samples_do_not_trigger_anything():
    assert evaluate(SendStats(sent=20, hard_bounces=2)).action is Action.NONE


def test_three_percent_bounces_quarantines_the_mailbox():
    d = evaluate(SendStats(sent=200, hard_bounces=6, replies=5))
    assert d.action is Action.QUARANTINE_MAILBOX


def test_two_percent_bounces_only_throttles():
    d = evaluate(SendStats(sent=200, hard_bounces=4, replies=5))
    assert d.action is Action.THROTTLE


def test_esp_complaint_threshold_is_ten_times_stricter():
    """0.2% is survivable on your own mailbox and fatal on a shared ESP pool."""
    stats = dict(sent=1000, complaints=2, replies=10)
    assert evaluate(SendStats(**stats, family="mailbox")).action is Action.NONE
    assert evaluate(SendStats(**stats, family="esp")).action is Action.PAUSE_CAMPAIGN


def test_auth_failure_pauses_immediately_regardless_of_sample_size():
    d = evaluate(SendStats(sent=3, auth_failures=1))
    assert d.action is Action.PAUSE_CAMPAIGN
    assert "unauthenticated" in d.reason


def test_silent_foldering_is_caught():
    """Nothing errors; the mail simply stops being seen."""
    assert evaluate(SendStats(sent=250, replies=0)).action is Action.THROTTLE


def test_healthy_campaign_is_left_alone():
    assert evaluate(SendStats(sent=500, hard_bounces=2, replies=30)).action is Action.NONE
