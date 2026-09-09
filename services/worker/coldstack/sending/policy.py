"""Campaign / transport compatibility gate.

The single rule that keeps a user from torching an ESP account:

    A campaign of class COLD may only bind mailbox-family transports.

Cold outreach means contacting someone who never asked to hear from you. Every ESP
acceptable-use policy forbids it — Resend, Mailchimp, SendGrid, Mailjet, Brevo and
the rest all prohibit unsolicited mail and purchased or scraped lists, and enforce by
account termination, usually without warning and often mid-campaign. Using them for
cold sequences also poisons a shared IP pool that other customers depend on.

ESPs are still genuinely useful here, for three campaign classes that are opt-in:
  OPT_IN      — people who subscribed (newsletters, nurture, product updates)
  TRANSACTIONAL — receipts, exports, password resets, system mail from the app itself
  WARM        — existing customers and prior contacts with a documented relationship

So this is not a blocklist. It is a router: the campaign declares intent, and the
transports that are legal for that intent are the only ones offered.
"""
from __future__ import annotations

from enum import Enum

from .base import Transport, TransportFamily


class CampaignClass(str, Enum):
    COLD = "cold"
    WARM = "warm"
    OPT_IN = "opt_in"
    TRANSACTIONAL = "transactional"

    def __str__(self) -> str:
        return self.value


class PolicyViolation(Exception):
    pass


_ALLOWED: dict[CampaignClass, set[TransportFamily]] = {
    CampaignClass.COLD: {TransportFamily.MAILBOX},
    CampaignClass.WARM: {TransportFamily.MAILBOX},
    CampaignClass.OPT_IN: {TransportFamily.MAILBOX, TransportFamily.ESP},
    CampaignClass.TRANSACTIONAL: {TransportFamily.ESP, TransportFamily.MAILBOX},
}


def assert_allowed(campaign_class: CampaignClass, transport: Transport) -> None:
    allowed = _ALLOWED[campaign_class]
    if transport.caps.family not in allowed:
        raise PolicyViolation(
            f"{transport.name} is a {transport.caps.family} transport and cannot be used "
            f"for a '{campaign_class}' campaign. "
            f"{transport.name}'s acceptable-use policy prohibits unsolicited outreach; "
            f"sending cold sequences through it risks account termination and burns a "
            f"shared IP pool. Use a mailbox transport (Gmail OAuth, Microsoft Graph, or "
            f"SMTP) for cold campaigns, or reclassify this campaign as 'opt_in' if the "
            f"recipients actually subscribed."
        )
    if campaign_class is CampaignClass.COLD and not transport.caps.cold_outreach_safe:
        raise PolicyViolation(f"{transport.name} is not cold-outreach safe.")


def eligible(campaign_class: CampaignClass, transports: dict[str, Transport]) -> dict[str, Transport]:
    """What the UI should offer for this campaign class. Filter, don't warn-after-the-fact."""
    return {
        n: t for n, t in transports.items()
        if t.caps.family in _ALLOWED[campaign_class]
        and (campaign_class is not CampaignClass.COLD or t.caps.cold_outreach_safe)
    }
