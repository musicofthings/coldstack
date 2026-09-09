# Sending engine + deliverability

The hardest and most defensible part of the build. Notes before code.

## Mailbox pool
A campaign does not own mailboxes; it draws from a workspace pool. Each mailbox row
carries: transport, credentials ref, `daily_cap`, `ramp_day`, current rolling stats, and
health status (`warming | healthy | throttled | quarantined`).

**Ramp curve.** Day 1: 5 sends. Increase ~30%/day to the cap over ~14–21 days. A new
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
