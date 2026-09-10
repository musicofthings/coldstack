import pytest

from coldstack.sending.policy import (CampaignClass, PolicyViolation, assert_allowed,
                                      eligible)
from coldstack.sending.registry import all_transports


@pytest.mark.parametrize("esp", ["resend", "mailchimp_transactional", "mailjet",
                                 "sendgrid", "postmark", "brevo", "mailgun",
                                 "mailersend", "smtp2go", "sparkpost"])
def test_no_esp_may_send_a_cold_campaign(esp):
    with pytest.raises(PolicyViolation):
        assert_allowed(CampaignClass.COLD, all_transports()[esp])


def test_mailbox_may_send_cold():
    assert_allowed(CampaignClass.COLD, all_transports()["smtp"])


def test_cold_eligibility_is_mailbox_only():
    """Asserts the property, not the membership. Hardcoding the set meant adding the
    Gmail transport - a correct change - failed this test for the wrong reason."""
    ts = all_transports()
    cold = eligible(CampaignClass.COLD, ts)
    assert cold, "no transport is usable for cold outreach"
    assert all(str(t.caps.family) == "mailbox" for t in cold.values())
    assert all(t.caps.cold_outreach_safe for t in cold.values())
    assert not any(str(ts[n].caps.family) == "esp" for n in cold)


def test_opt_in_may_use_every_transport():
    ts = all_transports()
    assert set(eligible(CampaignClass.OPT_IN, ts)) == set(ts)


def test_every_esp_is_flagged_not_cold_safe():
    for name, t in all_transports().items():
        if str(t.caps.family) == "esp":
            assert t.caps.cold_outreach_safe is False, name
