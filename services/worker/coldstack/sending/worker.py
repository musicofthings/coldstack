"""The send tick - where scheduler, sequences, transports and breakers meet.

One tick does the smallest safe unit of work: check whether we are allowed to send at
all, take the enrollments that are due, and for each one find a mailbox that may send
to that recipient's domain right now. Anything it cannot do it defers rather than
forces, because every "force it through" in a cold-email system is a reputation debt.

Ordering inside the tick is not arbitrary:

  breakers -> policy -> due -> suppression -> mailbox -> send -> advance

Breakers first, so a campaign that is already in trouble sends nothing further while
we work out the details. Policy second, so a misconfigured campaign fails before it
touches a transport. Suppression before mailbox selection, so a suppressed contact
never consumes a mailbox's daily allowance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .base import SendRequest, Transport
from .breakers import Action, SendStats, evaluate
from .policy import CampaignClass, assert_allowed
from .scheduler import (Mailbox, NoMailboxAvailable, SendWindow, next_slot,
                        pick_mailbox, record_send)
from .sequences import (Enrollment, Outcome, Sequence, advance, decide,
                        due_enrollments)


@dataclass(slots=True)
class CampaignContext:
    campaign_id: str
    campaign_class: CampaignClass
    sequence: Sequence
    mailboxes: list[Mailbox]
    transport: Transport
    creds: dict[str, str]
    window: SendWindow = field(default_factory=SendWindow)
    suppressed_emails: set[str] = field(default_factory=set)
    suppressed_domains: set[str] = field(default_factory=set)
    stats: SendStats = field(default_factory=SendStats)
    from_name: str | None = None
    reply_to: str | None = None


@dataclass(slots=True)
class TickResult:
    sent: int = 0
    deferred: int = 0
    suppressed: int = 0
    completed: int = 0
    failed: int = 0
    paused: bool = False
    pause_reason: str = ""
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.paused:
            return f"PAUSED: {self.pause_reason}"
        return (f"sent={self.sent} deferred={self.deferred} suppressed={self.suppressed} "
                f"completed={self.completed} failed={self.failed}")


def _render(template: str, lead: dict[str, str]) -> str:
    """Minimal, forgiving substitution. An unknown token is left visible rather than
    silently blanked - "Hi ," reaching a prospect is worse than an obvious {{name}}."""
    out = template
    for key, value in lead.items():
        out = out.replace("{{" + key + "}}", value or "")
    return out


async def tick(now: datetime, enrollments: list[Enrollment], ctx: CampaignContext,
               lead_fields: dict[str, dict[str, str]] | None = None,
               *, dry_run: bool = False, limit: int = 100) -> TickResult:
    result = TickResult()
    lead_fields = lead_fields or {}

    decision = evaluate(ctx.stats)
    if decision.action in (Action.PAUSE_CAMPAIGN, Action.QUARANTINE_MAILBOX):
        return TickResult(paused=True, pause_reason=f"{decision.action}: {decision.reason}")
    if decision.action is Action.THROTTLE:
        limit = max(1, limit // 4)
        result.notes.append(f"throttled: {decision.reason}")

    # A misconfigured campaign must fail here, not at the transport.
    assert_allowed(ctx.campaign_class, ctx.transport)

    for enrollment in due_enrollments(enrollments, now)[:limit]:
        d = decide(enrollment, ctx.sequence, now,
                   suppressed_emails=ctx.suppressed_emails,
                   suppressed_domains=ctx.suppressed_domains)

        if d.outcome is Outcome.SKIP_SUPPRESSED:
            result.suppressed += 1
            continue
        if d.outcome is Outcome.COMPLETE:
            result.completed += 1
            continue
        if d.outcome in (Outcome.SKIP_NOT_DUE, Outcome.SKIP_INACTIVE):
            result.deferred += 1
            continue

        domain = enrollment.contact_email.rpartition("@")[2]
        try:
            mailbox = pick_mailbox(ctx.mailboxes, domain, now)
        except NoMailboxAvailable as exc:
            # Not an error. Capacity is finite by design; the next tick tries again.
            result.deferred += 1
            if str(exc) not in result.notes:
                result.notes.append(str(exc))
            continue

        when = next_slot(mailbox, now, ctx.window)
        if when > now:
            # The slot is in the future (pacing gap or outside the send window).
            enrollment.next_send_at = when
            result.deferred += 1
            continue

        fields = lead_fields.get(enrollment.contact_email, {})
        variant = d.variant
        req = SendRequest(
            from_email=mailbox.email, from_name=ctx.from_name,
            to_email=enrollment.contact_email, to_name=fields.get("full_name"),
            subject=_render(variant.subject or "", fields),
            text=_render(variant.body, fields),
            reply_to=ctx.reply_to,
            in_reply_to=enrollment.last_message_id if d.step.same_thread else None,
            references=list(enrollment.references) if d.step.same_thread else [],
            headers={"List-Unsubscribe": "<mailto:unsubscribe@example.com>",
                     "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
            idempotency_key=f"{ctx.campaign_id}:{enrollment.id}:{enrollment.current_step}",
        )

        if dry_run:
            result.sent += 1
            record_send(mailbox, domain, now)
            advance(enrollment, ctx.sequence, now, f"<dry-{req.idempotency_key}>")
            continue

        res = await ctx.transport.send(req, creds=ctx.creds)
        if not res.ok:
            result.failed += 1
            ctx.stats.sent += 1
            if not res.retryable:
                ctx.stats.hard_bounces += 1
            result.notes.append(res.error or "send failed")
            continue

        result.sent += 1
        ctx.stats.sent += 1
        record_send(mailbox, domain, now)
        advance(enrollment, ctx.sequence, now,
                res.message_id_hdr or res.provider_message_id or "")

    return result
