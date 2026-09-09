"""Turn a classified inbound message into state changes.

Deliberately returns an InboxAction rather than mutating a database directly, so the
decision is testable and the caller owns persistence. The ordering below is the whole
point of the module:

    classify -> match -> act

A message that cannot be matched to an enrollment is still recorded, and a hard bounce
still suppresses the address even when we cannot say which campaign it came from -
dropping an unmatched bounce means sending to a dead address again next week.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from email.message import Message

from ..sending.sequences import (Enrollment, EnrollmentStatus, on_bounce, on_reply,
                                 on_unsubscribe)
from .classify import Classification, Kind, classify
from .threading import Match, match_enrollment


@dataclass(slots=True)
class InboxAction:
    classification: Classification
    match: Match
    stopped_enrollments: list[str] = field(default_factory=list)
    suppress_email: str | None = None
    retry_after_days: int | None = None
    note: str = ""


def process(msg: Message, enrollments: list[Enrollment], *,
            sent_index: dict[str, str], address_index: dict[str, str]) -> InboxAction:
    c = classify(msg)
    m = match_enrollment(msg, sent_index=sent_index, address_index=address_index,
                         bounced_address=c.bounced_address)

    by_id = {e.id: e for e in enrollments}
    target = by_id.get(m.enrollment_id) if m.enrollment_id else None
    action = InboxAction(classification=c, match=m)

    if c.kind is Kind.HUMAN_REPLY:
        if target:
            # Stops every active enrollment for this person, not just this campaign.
            stopped = on_reply(enrollments, target.contact_email)
            action.stopped_enrollments = [e.id for e in stopped]
        else:
            action.note = "human reply could not be matched to an enrollment"

    elif c.kind is Kind.UNSUBSCRIBE:
        email = (target.contact_email if target else c.bounced_address
                 or _sender(msg))
        if email:
            on_unsubscribe(enrollments, email)
            action.suppress_email = email.lower()
            action.stopped_enrollments = [e.id for e in enrollments
                                          if e.contact_email.lower() == email.lower()
                                          and e.status is EnrollmentStatus.UNSUBSCRIBED]

    elif c.kind is Kind.BOUNCE_HARD:
        email = (c.bounced_address or (target.contact_email if target else "")).lower()
        # A dead address is dead for every campaign, so stop them all - not just the
        # one the address index happened to resolve to. The index is a dict keyed by
        # email, so with two enrollments for one person it can only name one of them.
        if email:
            for e in enrollments:
                if e.contact_email.lower() == email and e.is_active:
                    on_bounce(e, hard=True)
                    action.stopped_enrollments.append(e.id)
        elif target:
            on_bounce(target, hard=True)
            action.stopped_enrollments = [target.id]
        # Suppress even with no match: the address is dead regardless of which
        # campaign discovered it.
        action.suppress_email = email or None
        if not action.stopped_enrollments:
            action.note = "hard bounce suppressed without a matching enrollment"

    elif c.kind is Kind.BOUNCE_SOFT:
        # Transient. Do not suppress, do not stop - back off and try again.
        action.retry_after_days = 2
        action.note = "soft bounce - retrying later"

    elif c.kind in (Kind.OUT_OF_OFFICE, Kind.AUTO_REPLY):
        # Explicitly does NOT stop the sequence. An OOO means "later", not "no", and
        # treating it as a reply quietly drops a live prospect who was on holiday.
        action.retry_after_days = 5 if c.kind is Kind.OUT_OF_OFFICE else None
        action.note = f"{c.kind} - sequence continues"

    return action


def _sender(msg: Message) -> str | None:
    from .threading import _extract_address
    return _extract_address(msg.get("From") or "")
