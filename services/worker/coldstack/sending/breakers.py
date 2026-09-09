"""Circuit breakers.

Cold email fails slowly and then all at once. By the time a human notices reply rates
dropping, the domain is already damaged. These thresholds stop a campaign or quarantine
a mailbox automatically, because the whole failure mode is that nobody is watching.

Thresholds are deliberately different per transport family: on a bulk ESP a 0.1%
complaint rate is an emergency, an order of magnitude stricter than what a mailbox
tolerates, because you are sharing an IP pool with strangers.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

HARD_BOUNCE_PAUSE = 0.03          # 3% of the last N sends
HARD_BOUNCE_WARN = 0.02
COMPLAINT_PAUSE_MAILBOX = 0.003
COMPLAINT_PAUSE_ESP = 0.001       # ESPs terminate accounts around here
MIN_SAMPLE = 50                   # below this, rates are noise
SILENT_REPLY_SAMPLE = 200         # zero replies over this many sends = foldering


class Action(str, Enum):
    NONE = "none"
    WARN = "warn"
    THROTTLE = "throttle"
    PAUSE_CAMPAIGN = "pause_campaign"
    QUARANTINE_MAILBOX = "quarantine_mailbox"

    def __str__(self) -> str:
        return self.value


@dataclass(slots=True)
class SendStats:
    sent: int = 0
    hard_bounces: int = 0
    complaints: int = 0
    replies: int = 0
    auth_failures: int = 0
    family: str = "mailbox"       # "mailbox" | "esp"

    @property
    def bounce_rate(self) -> float:
        return self.hard_bounces / self.sent if self.sent else 0.0

    @property
    def complaint_rate(self) -> float:
        return self.complaints / self.sent if self.sent else 0.0


@dataclass(slots=True)
class Decision:
    action: Action
    reason: str


def evaluate(stats: SendStats) -> Decision:
    # Authentication breaking mid-campaign means every subsequent send is unsigned.
    # This one does not wait for a sample size.
    if stats.auth_failures > 0:
        return Decision(Action.PAUSE_CAMPAIGN,
                        f"{stats.auth_failures} authentication failure(s): SPF/DKIM broke "
                        f"mid-campaign. Every further send is unauthenticated.")

    if stats.sent < MIN_SAMPLE:
        return Decision(Action.NONE, f"only {stats.sent} sends - below the {MIN_SAMPLE} "
                                     f"needed for a rate to mean anything")

    complaint_limit = (COMPLAINT_PAUSE_ESP if stats.family == "esp"
                       else COMPLAINT_PAUSE_MAILBOX)
    if stats.complaint_rate >= complaint_limit:
        return Decision(Action.PAUSE_CAMPAIGN,
                        f"complaint rate {stats.complaint_rate:.2%} at or above the "
                        f"{complaint_limit:.2%} limit for a {stats.family} sender")

    if stats.bounce_rate >= HARD_BOUNCE_PAUSE:
        return Decision(Action.QUARANTINE_MAILBOX,
                        f"hard bounce rate {stats.bounce_rate:.1%} at or above "
                        f"{HARD_BOUNCE_PAUSE:.0%} - the list is stale or unverified")

    if stats.bounce_rate >= HARD_BOUNCE_WARN:
        return Decision(Action.THROTTLE,
                        f"hard bounce rate {stats.bounce_rate:.1%} approaching the "
                        f"{HARD_BOUNCE_PAUSE:.0%} limit")

    # Sends continuing with zero replies usually means silent spam-foldering: nothing
    # errors, the mail simply stops being seen.
    if stats.sent >= SILENT_REPLY_SAMPLE and stats.replies == 0:
        return Decision(Action.THROTTLE,
                        f"{stats.sent} sends and zero replies - likely being filtered "
                        f"to spam rather than rejected")

    return Decision(Action.NONE, "within thresholds")
