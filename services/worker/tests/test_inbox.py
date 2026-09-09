"""Inbound classification is the highest-consequence guesswork in the system: getting
it wrong either drops a live prospect (OOO read as a reply) or emails someone who
already answered (reply read as noise). These use real message structures."""
from email import message_from_string

import pytest

from coldstack.inbox.classify import Intent, Kind, classify
from coldstack.inbox.process import process
from coldstack.inbox.threading import chain, match_enrollment
from coldstack.sending.sequences import Enrollment, EnrollmentStatus


def m(raw: str):
    return message_from_string(raw.strip() + "\n")


HUMAN = """From: Ana Cruz <ana@target.com>
To: me@sender.com
Subject: Re: Quick question
In-Reply-To: <out-1@sender.com>
References: <out-1@sender.com>
Content-Type: text/plain

Thanks for reaching out - this is interesting, can you send more detail?
"""

OOO_HEADER = """From: Ana Cruz <ana@target.com>
To: me@sender.com
Subject: Automatic reply: Quick question
Auto-Submitted: auto-replied
In-Reply-To: <out-1@sender.com>
Content-Type: text/plain

I am out of the office until 14 September with limited access to email.
"""

OOO_SUBJECT_ONLY = """From: Ana Cruz <ana@target.com>
To: me@sender.com
Subject: Out of office
In-Reply-To: <out-1@sender.com>
Content-Type: text/plain

Back Monday.
"""

HARD_BOUNCE = """From: Mail Delivery Subsystem <MAILER-DAEMON@target.com>
To: me@sender.com
Subject: Undelivered Mail Returned to Sender
Content-Type: multipart/report; report-type=delivery-status; boundary="b1"

--b1
Content-Type: text/plain

Your message could not be delivered. User unknown.

--b1
Content-Type: message/delivery-status

Reporting-MTA: dns; mx.target.com
Final-Recipient: rfc822; ana@target.com
Action: failed
Status: 5.1.1

--b1--
"""

SOFT_BOUNCE = """From: Mail Delivery Subsystem <MAILER-DAEMON@target.com>
To: me@sender.com
Subject: Delivery delayed
Content-Type: multipart/report; report-type=delivery-status; boundary="b1"

--b1
Content-Type: text/plain

Mailbox full, will retry.

--b1
Content-Type: message/delivery-status

Final-Recipient: rfc822; ana@target.com
Action: delayed
Status: 4.2.2

--b1--
"""

UNSUB = """From: Ana Cruz <ana@target.com>
To: me@sender.com
Subject: Re: Quick question
In-Reply-To: <out-1@sender.com>
Content-Type: text/plain

Please unsubscribe me and do not contact me again.
"""

VACATION_NULL_PATH = """From: Ana Cruz <ana@target.com>
To: me@sender.com
Return-Path: <>
Subject: Re: Quick question
Content-Type: text/plain

I am on annual leave, returning next week.
"""


# ------------------------------------------------------------ the core distinction

def test_a_human_reply_is_a_human_reply():
    c = classify(m(HUMAN))
    assert c.kind is Kind.HUMAN_REPLY
    assert c.stops_sequence is True


@pytest.mark.parametrize("raw", [OOO_HEADER, OOO_SUBJECT_ONLY, VACATION_NULL_PATH])
def test_out_of_office_never_stops_the_sequence(raw):
    """An OOO means 'later', not 'no'. Stopping here quietly drops a live prospect."""
    c = classify(m(raw))
    assert c.kind is Kind.OUT_OF_OFFICE
    assert c.stops_sequence is False
    assert c.suppresses is False


def test_header_evidence_beats_subject_guessing():
    """A person can type 'Out of office' in a real reply, so the header-backed case is
    high confidence and the subject-only case deliberately is not."""
    assert classify(m(OOO_HEADER)).confidence > classify(m(OOO_SUBJECT_ONLY)).confidence


def test_auto_submitted_no_is_not_an_auto_reply():
    """RFC 3834 allows Auto-Submitted: no on ordinary mail - it must not trigger."""
    raw = HUMAN.replace("Subject: Re:", "Auto-Submitted: no\nSubject: Re:")
    assert classify(m(raw)).kind is Kind.HUMAN_REPLY


# ------------------------------------------------------------ bounces

def test_hard_bounce_reads_the_enhanced_status_and_the_failed_recipient():
    c = classify(m(HARD_BOUNCE))
    assert c.kind is Kind.BOUNCE_HARD
    assert c.bounced_address == "ana@target.com"
    assert c.suppresses is True


def test_soft_bounce_does_not_suppress():
    c = classify(m(SOFT_BOUNCE))
    assert c.kind is Kind.BOUNCE_SOFT
    assert c.suppresses is False
    assert c.stops_sequence is False


def test_bounce_without_an_enhanced_status_falls_back_to_phrases():
    raw = HARD_BOUNCE.replace("Status: 5.1.1", "")
    assert classify(m(raw)).kind is Kind.BOUNCE_HARD


# ------------------------------------------------------------ opt-out and intent

def test_unsubscribe_beats_intent_analysis():
    c = classify(m(UNSUB))
    assert c.kind is Kind.UNSUBSCRIBE and c.suppresses


@pytest.mark.parametrize("body,expected", [
    ("This is interesting, can you send more?", Intent.POSITIVE),
    ("Not interested, we're all set thanks.", Intent.OBJECTION),
    ("I'm not the right person - speak to Dana who owns this.", Intent.REFERRAL),
    ("Received, thanks.", Intent.NEUTRAL),
])
def test_intent_inference(body, expected):
    raw = HUMAN.rsplit("\n\n", 1)[0] + "\n\n" + body
    assert classify(m(raw)).intent is expected


def test_vacation_wording_in_the_body_is_still_an_ooo():
    """Regression: most responders leave the subject as a plain 'Re: ...' and put the
    wording in the body. Reading only the subject cost those their retry."""
    from coldstack.inbox.classify import classify as c
    assert c(m(VACATION_NULL_PATH)).kind is Kind.OUT_OF_OFFICE


def test_referral_is_not_mistaken_for_an_objection():
    """'not the right person' contains a negation but is a good outcome, so referral
    must be checked before objection."""
    raw = HUMAN.rsplit("\n\n", 1)[0] + "\n\nI'm not the right person, not interested myself - speak to Dana."
    assert classify(m(raw)).intent is Intent.REFERRAL


# ------------------------------------------------------------ threading

def test_chain_prefers_in_reply_to_then_references():
    msg = m("""From: a@b.com
In-Reply-To: <c@d.com>
References: <x@y.com> <c@d.com>

hi""")
    assert chain(msg)[0] == "<c@d.com>"
    assert "<x@y.com>" in chain(msg)


def test_match_by_chain_beats_match_by_address():
    r = match_enrollment(m(HUMAN), sent_index={"<out-1@sender.com>": "e-chain"},
                         address_index={"ana@target.com": "e-addr"})
    assert r.enrollment_id == "e-chain" and r.method == "chain"


def test_bounce_matches_on_the_reported_recipient_not_the_daemon():
    """A DSN comes from MAILER-DAEMON and carries no References - matching on the
    From address would find nothing."""
    r = match_enrollment(m(HARD_BOUNCE), sent_index={},
                         address_index={"ana@target.com": "e1"},
                         bounced_address="ana@target.com")
    assert r.enrollment_id == "e1" and r.method == "bounced_address"


# ------------------------------------------------------------ end to end

def enrolls():
    return [Enrollment(id="e1", contact_email="ana@target.com"),
            Enrollment(id="e2", contact_email="ana@target.com"),   # another campaign
            Enrollment(id="e3", contact_email="bob@target.com")]


def idx(es):
    return {"<out-1@sender.com>": "e1"}, {e.contact_email: e.id for e in es}


def test_a_reply_stops_every_campaign_for_that_person():
    es = enrolls()
    sent, addr = idx(es)
    a = process(m(HUMAN), es, sent_index=sent, address_index=addr)
    assert set(a.stopped_enrollments) == {"e1", "e2"}
    assert es[2].is_active                                    # different person


def test_an_ooo_stops_nothing_and_schedules_a_retry():
    es = enrolls()
    sent, addr = idx(es)
    a = process(m(OOO_HEADER), es, sent_index=sent, address_index=addr)
    assert a.stopped_enrollments == []
    assert all(e.is_active for e in es)
    assert a.retry_after_days == 5


def test_hard_bounce_stops_every_enrollment_for_that_address():
    """Regression: the address index is keyed by email, so with two campaigns
    targeting one person it can only name one of them. A dead address is dead for
    every campaign."""
    es = enrolls()
    sent, addr = idx(es)
    a = process(m(HARD_BOUNCE), es, sent_index=sent, address_index=addr)
    assert a.suppress_email == "ana@target.com"
    assert set(a.stopped_enrollments) == {"e1", "e2"}
    assert es[0].status is EnrollmentStatus.BOUNCED
    assert es[1].status is EnrollmentStatus.BOUNCED
    assert es[2].is_active                                    # different person


def test_unmatched_hard_bounce_still_suppresses():
    """Dropping it means sending to a dead address again next week."""
    a = process(m(HARD_BOUNCE), [], sent_index={}, address_index={})
    assert a.suppress_email == "ana@target.com"
    assert "without a matching enrollment" in a.note


def test_soft_bounce_leaves_everything_alone():
    es = enrolls()
    sent, addr = idx(es)
    a = process(m(SOFT_BOUNCE), es, sent_index=sent, address_index=addr)
    assert a.suppress_email is None and a.stopped_enrollments == []
    assert all(e.is_active for e in es)


# ------------------------------------------------------------ imap fetch

class FakeImap:
    """Minimal IMAP double. Records what was called so we can assert we never mutate
    the user's mailbox."""
    def __init__(self, uids, raw):
        self.uids, self.raw, self.calls, self.readonly = uids, raw, [], None

    def select(self, folder, readonly=False):
        self.readonly = readonly
        self.calls.append(("select", folder))
        return "OK", [b""]

    def uid(self, command, *args):
        self.calls.append((command, args))
        if command == "search":
            return "OK", [" ".join(str(u) for u in self.uids).encode()]
        if command == "fetch":
            uid = int(args[0])
            return "OK", [(b"", self.raw.encode())]
        return "NO", [b""]

    def logout(self):
        self.calls.append(("logout", ()))


def test_fetch_is_readonly_and_never_flags_the_mailbox():
    from coldstack.inbox.sync import ImapConfig, fetch_since
    fake = FakeImap([1, 2, 3], HUMAN)
    r = fetch_since(ImapConfig("h", "u", "p"), since_uid=0, conn=fake)
    assert fake.readonly is True
    assert not any(c[0] == "store" for c in fake.calls)
    assert r.last_uid == 3 and len(r.messages) == 3


def test_fetch_filters_uids_at_or_below_the_cursor():
    """IMAP's 'UID n:*' always returns the final message even when its UID is below
    the range, so the same reply would be reprocessed on every poll."""
    from coldstack.inbox.sync import ImapConfig, fetch_since
    fake = FakeImap([7], HUMAN)
    r = fetch_since(ImapConfig("h", "u", "p"), since_uid=9, conn=fake)
    assert r.messages == [] and r.last_uid == 9


def test_connection_failure_is_reported_not_raised():
    from coldstack.inbox.sync import ImapConfig, fetch_since

    class Broken(FakeImap):
        def select(self, folder, readonly=False):
            raise OSError("connection reset")

    r = fetch_since(ImapConfig("h", "u", "p"), conn=Broken([], ""))
    assert r.messages == [] and "connection reset" in r.error
