# Sending engine + deliverability

The hardest and most defensible part of the build. Notes before code.

## Mailbox pool
A campaign does not own mailboxes; it draws from a workspace pool. Each mailbox row
carries: transport, credentials ref, `daily_cap`, `ramp_day`, current rolling stats, and
health status (`warming | healthy | throttled | quarantined`).

**Ramp curve.** Day 1: 5 sends. Increase ~15%/day to the cap over ~14–21 days.
(30%/day, the figure often quoted, reaches a 40/day cap in 8 days — too fast,
and inconsistent with the 14–21 day guidance it is usually quoted alongside.) A new
domain that sends 500 on day 3 is dead. Enforce this in code — do not make it advisory.

**Rotation.** Round-robin across healthy mailboxes with jitter, weighted by remaining
daily budget. Never send two messages from the same mailbox to the same domain within a
short window.

**Human-shaped timing.** Randomised gaps (90–600s), recipient-local business hours,
weekday-only by default, no round-number send times.

## Warmup
Peer-to-peer pool: participating mailboxes exchange conversational messages, open, reply,
and drag from spam to inbox. Keep the warmup corpus varied and the reply threads real —
templated warmup text is itself a fingerprint. Warmup traffic must be tagged in headers
so it never pollutes campaign analytics.

## Circuit breakers (non-negotiable)
Auto-pause a campaign or quarantine a mailbox when:
- rolling hard-bounce rate > 3% over the last 200 sends
- spam-complaint proxy (unsubscribe + negative-reply rate) spikes
- authentication fails on the sending domain (DKIM/SPF break mid-campaign)
- a mailbox's replies stop entirely while sends continue (silent spam-foldering)

## DNS doctor
Per sending domain, continuously checked: MX present; SPF exists, single record, within
10 lookups; DKIM selector resolves with ≥1024-bit key; DMARC present and at least
`p=none` with `rua`; tracking domain on a **separate** domain from the sending domain;
PTR set if self-hosting SMTP; Spamhaus DBL/ZEN lookups clean.

## Reply handling
IMAP IDLE where available, polling otherwise. Thread by `Message-ID`/`References`, not
subject. Classify: positive / neutral / objection / referral / unsubscribe / auto-reply /
out-of-office / bounce. Auto-replies must not stop the sequence; real replies must stop it
immediately, including for other campaigns targeting the same person.

## Compliance — build it in, do not bolt it on
- **CAN-SPAM (US):** accurate headers, physical postal address, working opt-out honoured
  within 10 days.
- **GDPR (EU/UK):** legitimate-interest basis, B2B-role addresses only, immediate opt-out,
  data-subject access and erasure endpoints. Dropcontact is the cleanest EU-side vendor.
- **CASL (Canada):** effectively opt-in. Default-exclude Canadian contacts unless the user
  explicitly overrides with a documented basis.
- **India DPDP Act 2023:** consent/notice obligations; keep processing records and honour
  erasure. Relevant given self-hosting in India.
- Ship a **global suppression list**, one-click unsubscribe (RFC 8058 `List-Unsubscribe`
  headers), and a per-workspace data-deletion job. Never make these optional.

---

# Transports: mailboxes vs ESPs

ColdStack supports eleven transports in two families. The distinction is not cosmetic —
it decides what a campaign is legally and operationally allowed to do.

| Transport | Family | Cold-safe | Batch | Replies back? | Note |
|---|---|:--:|--:|:--:|---|
| smtp (+ gmail_oauth, ms_graph in P2) | mailbox | **yes** | 1 | IMAP | ~40/day/mailbox after ramp |
| resend | esp | no | 100 | webhook only | clean API, no inbound product |
| mailjet | esp | no | 50 | Parse API | Basic auth, key:secret |
| mailchimp_transactional (Mandrill) | esp | no | 1 | no | key goes in the body; needs the Transactional add-on |
| sendgrid | esp | no | 1000 | Inbound Parse | most aggressive cold-email enforcement |
| postmark | esp | no | 500 | inbound streams | strictest AUP, manual review |
| brevo | esp | no | 1 | yes | EU residency, generous free tier |
| mailgun | esp | no | 1000 | Routes | form-encoded, EU region host differs |
| mailersend | esp | no | 500 | yes | |
| smtp2go | esp | no | 1 | no | more tolerant AUP, still not a licence to cold-email |
| sparkpost | esp | no | 1000 | Relay webhooks | |

## Why every ESP is marked `cold_outreach_safe = False`

This is their acceptable-use policy, not our opinion. Resend, Mailchimp, SendGrid,
Mailjet, Brevo and the rest all prohibit unsolicited mail and purchased or scraped
lists, and they enforce by **account termination — usually without warning and often
mid-campaign**. SendGrid in particular is documented as killing accounts on first
signal. Beyond your own account, cold volume on a shared IP pool degrades deliverability
for every other customer on it, which is why enforcement is so blunt.

So the rule is a router, not a blocklist:

```
COLD          -> mailbox transports only
WARM          -> mailbox transports only
OPT_IN        -> mailbox or ESP
TRANSACTIONAL -> ESP or mailbox
```

Enforced twice, on purpose: in `coldstack/sending/policy.py` for a good error message
at bind time, and again as a Postgres trigger on `campaign_sender` (migration 0002),
because an application bug here costs a user their sending account.

## What the ESPs are genuinely for
1. **The product's own system mail** — exports ready, invites, password resets. You need
   a transactional sender regardless, and Postmark or Resend is the right tool.
2. **Opt-in nurture after the cold sequence works.** A reply-to-subscribe list, a
   newsletter, product updates. This is the natural second act for a cold-email tool and
   the reason to build the ESP layer now rather than later.
3. **Warm re-engagement of existing customers**, where a documented relationship exists.
4. **Scale that mailboxes cannot reach.** 40/day/mailbox means 10k/day needs ~250
   mailboxes. For opt-in audiences an ESP does that with one connection.

## ESP-specific mechanics worth knowing
- **No inbox.** An ESP send is fire-and-forget. Always set `Reply-To` to a real mailbox
  you can IMAP, or configure the vendor's inbound route to `/webhooks/inbound/{provider}`.
- **Threading is weaker.** Most ESPs accept custom `In-Reply-To`/`References` headers,
  but they rewrite `Message-ID` — so store `provider_message_id` alongside the header
  (`message.provider_message_id`, added in 0002) and reconcile on webhook events.
- **Events, not polling.** Bounces and complaints arrive as webhooks into `esp_event`.
  The same circuit breakers apply: a complaint rate above ~0.1% is an emergency on an
  ESP, an order of magnitude stricter than the mailbox bounce threshold.
- **Domain separation.** Never send opt-in ESP volume from the same domain as cold
  mailbox volume. Separate domains, separate DKIM selectors, separate reputations.
