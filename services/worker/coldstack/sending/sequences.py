"""Sequence engine: enrollment state machine and step scheduling.

A campaign is a list of steps; an enrollment is one contact walking through them. The
engine is pure - it computes what should happen and when, and returns decisions. The
worker performs them. That split is what makes multi-week sequence behaviour testable
in milliseconds instead of weeks.

The rules that matter, in the order they are checked:

1. **A reply ends everything.** Not just this campaign - every active enrollment for
   that person, across campaigns. Someone who answered your first email and then gets
   step 3 of a different sequence has learned exactly what you are.
2. **Suppression is checked at send time, never at enrolment time.** Lists go stale;
   an unsubscribe that arrives after enrolment must still stop the next send.
3. **Bounces stop the enrollment immediately** and add the address to suppression.
4. **Variants are assigned deterministically** from the enrollment id, so a retry or a
   restart never re-rolls a contact into the other arm and corrupts the A/B result.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


class EnrollmentStatus(str, Enum):
    ACTIVE = "active"
    REPLIED = "replied"
    BOUNCED = "bounced"
    UNSUBSCRIBED = "unsubscribed"
    COMPLETED = "completed"
    SUPPRESSED = "suppressed"

    def __str__(self) -> str:
        return self.value


TERMINAL = {EnrollmentStatus.REPLIED, EnrollmentStatus.BOUNCED,
            EnrollmentStatus.UNSUBSCRIBED, EnrollmentStatus.COMPLETED,
            EnrollmentStatus.SUPPRESSED}


@dataclass(slots=True)
class Variant:
    id: str
    label: str
    subject: str | None
    body: str
    weight: float = 1.0


@dataclass(slots=True)
class Step:
    index: int
    variants: list[Variant]
    delay_days: int = 3
    same_thread: bool = True

    def variant_for(self, enrollment_id: str) -> Variant:
        """Deterministic weighted assignment.

        Hashing the enrollment id rather than drawing at random means a retried send,
        a restarted worker, or a re-read from the database all pick the same arm. A
        random draw per attempt would quietly contaminate every A/B result.
        """
        if len(self.variants) == 1:
            return self.variants[0]
        digest = hashlib.sha256(f"{enrollment_id}:{self.index}".encode()).digest()
        point = int.from_bytes(digest[:8], "big") / float(1 << 64)
        total = sum(v.weight for v in self.variants) or 1.0
        cursor = 0.0
        for v in self.variants:
            cursor += v.weight / total
            if point < cursor:
                return v
        return self.variants[-1]


@dataclass(slots=True)
class Sequence:
    steps: list[Step]

    def step(self, index: int) -> Step | None:
        return next((s for s in self.steps if s.index == index), None)


@dataclass(slots=True)
class Enrollment:
    id: str
    contact_email: str
    status: EnrollmentStatus = EnrollmentStatus.ACTIVE
    current_step: int = 0
    next_send_at: datetime | None = None
    thread_id: str | None = None
    last_message_id: str | None = None
    references: list[str] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.status is EnrollmentStatus.ACTIVE


class Outcome(str, Enum):
    SEND = "send"
    SKIP_SUPPRESSED = "skip_suppressed"
    SKIP_NOT_DUE = "skip_not_due"
    SKIP_INACTIVE = "skip_inactive"
    COMPLETE = "complete"

    def __str__(self) -> str:
        return self.value


@dataclass(slots=True)
class SendDecision:
    outcome: Outcome
    enrollment: Enrollment
    step: Step | None = None
    variant: Variant | None = None
    reason: str = ""


def decide(enrollment: Enrollment, sequence: Sequence, now: datetime,
           *, suppressed_emails: set[str], suppressed_domains: set[str]) -> SendDecision:
    if not enrollment.is_active:
        return SendDecision(Outcome.SKIP_INACTIVE, enrollment,
                            reason=f"enrollment is {enrollment.status}")

    email = enrollment.contact_email.lower()
    domain = email.rpartition("@")[2]
    # Checked here, at send time, not at enrolment - a list built last week does not
    # know about an unsubscribe that arrived yesterday.
    if email in suppressed_emails or domain in suppressed_domains:
        enrollment.status = EnrollmentStatus.SUPPRESSED
        return SendDecision(Outcome.SKIP_SUPPRESSED, enrollment,
                            reason=f"{email} is suppressed")

    step = sequence.step(enrollment.current_step)
    if step is None:
        enrollment.status = EnrollmentStatus.COMPLETED
        return SendDecision(Outcome.COMPLETE, enrollment, reason="no further steps")

    if enrollment.next_send_at and now < enrollment.next_send_at:
        return SendDecision(Outcome.SKIP_NOT_DUE, enrollment, step=step,
                            reason=f"due at {enrollment.next_send_at.isoformat()}")

    return SendDecision(Outcome.SEND, enrollment, step=step,
                        variant=step.variant_for(enrollment.id))


def advance(enrollment: Enrollment, sequence: Sequence, sent_at: datetime,
            message_id: str) -> None:
    """Record a successful send and schedule the next step."""
    step = sequence.step(enrollment.current_step)
    if step and step.same_thread:
        enrollment.thread_id = enrollment.thread_id or message_id
        enrollment.references.append(message_id)
    enrollment.last_message_id = message_id

    nxt = sequence.step(enrollment.current_step + 1)
    enrollment.current_step += 1
    if nxt is None:
        enrollment.status = EnrollmentStatus.COMPLETED
        enrollment.next_send_at = None
    else:
        enrollment.next_send_at = sent_at + timedelta(days=nxt.delay_days)


def on_reply(enrollments: list[Enrollment], contact_email: str) -> list[Enrollment]:
    """A reply stops every active enrollment for that person, in every campaign."""
    email = contact_email.lower()
    stopped = []
    for e in enrollments:
        if e.contact_email.lower() == email and e.is_active:
            e.status = EnrollmentStatus.REPLIED
            e.next_send_at = None
            stopped.append(e)
    return stopped


def on_bounce(enrollment: Enrollment, *, hard: bool = True) -> str | None:
    """Returns an address to add to suppression, if this bounce warrants it."""
    if not hard:
        return None
    enrollment.status = EnrollmentStatus.BOUNCED
    enrollment.next_send_at = None
    return enrollment.contact_email.lower()


def on_unsubscribe(enrollments: list[Enrollment], contact_email: str) -> str:
    email = contact_email.lower()
    for e in enrollments:
        if e.contact_email.lower() == email and e.is_active:
            e.status = EnrollmentStatus.UNSUBSCRIBED
            e.next_send_at = None
    return email


def due_enrollments(enrollments: list[Enrollment], now: datetime) -> list[Enrollment]:
    return [e for e in enrollments
            if e.is_active and (e.next_send_at is None or e.next_send_at <= now)]
