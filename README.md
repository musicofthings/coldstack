# ColdStack

An open-source, self-hostable, **BYOK** alternative to ListKit — the cold-outreach stack
without the $597/month and without the vendor lock on data.

> Working name. See `docs/` for the full teardown, architecture and roadmap.

## The idea in one paragraph
ListKit bundles a proprietary 977M-contact database with verification, inbox provisioning,
a sending engine and an AI copywriter. The database is the moat, and it is the one part an
open-source project cannot and should not clone. So ColdStack inverts it: you bring your
own keys (Apollo, People Data Labs, Hunter, Exa, an LLM, your own mailboxes) and ColdStack
owns the parts that are genuinely hard and that no vendor will sell you — a unified schema,
a cost-aware enrichment waterfall, identity resolution, real deliverability instrumentation,
and a sending engine that will not let you burn your domain.

## Status
Design + scaffold. Nothing runs end to end yet. Start with `docs/05-roadmap.md`.

## Documentation
| Doc | What's in it |
|---|---|
| `docs/00-feature-teardown.md` | What ListKit actually ships, module by module, and where it is weak |
| `docs/01-architecture.md` | Layer map, stack choices, credential vault, cost ledger |
| `docs/02-data-model.md` | The schema decisions worth defending |
| `docs/03-providers.md` | BYOK vendor landscape with costs; the derived-signals approach |
| `docs/04-sending-engine.md` | Warmup, rotation, circuit breakers, DNS doctor, compliance |
| `docs/05-roadmap.md` | Six phases, each a runnable vertical slice; licence; risks |

## Layout
```
apps/web              Next.js 15 + shadcn/ui
services/worker       Python 3.11, FastAPI + Celery  (package: coldstack)
packages/schema       shared TS types
db/migrations         plain SQL, applied on container init
infra                 deploy recipes
```

## Local dev (once P0 lands)
```bash
cp .env.example .env          # nothing is required to boot; keys are added per-workspace in the UI
docker compose up -d db redis # schema applies automatically from db/migrations
```
`docker compose up reacher` additionally runs a self-hosted SMTP verifier. It needs
outbound port 25, which most clouds block — run it on a VPS that allows it.

## Try it now (no keys, no cost)
The P0 slice runs end to end against deterministic fake providers:

```bash
cd services/worker
pip install -r requirements.txt
python -m coldstack.cli build --demo --limit 200 --only-valid --out leads.csv
```

A real run needs only a search key; every enrichment step is optional and priced:

```bash
export APOLLO_API_KEY=...  HUNTER_API_KEY=...
python -m coldstack.cli build \
  --title "Head of Laboratory" --title "Director of Genomics" \
  --country India --headcount 20-500 --limit 500 \
  --budget 15 --only-valid --suppress do-not-contact.txt --out leads.csv
```

`--budget` is a hard stop in dollars, and the run reports what it spent per provider:

```
searched          200
after dedupe      200
after suppression 200
emails found      161
verified valid    131
total cost        $0.8805
cost/valid email  $0.0067
providers:
  hunter            140/200  hits (70.0%)  $1.0000
  apollo_enrich      21/60   hits (35.0%)  $0.6300
  verify:hunter     161/161  hits (100.0%) $0.8050
```

## Run the app

```bash
# 1. API
cd services/worker && pip install -r requirements.txt
export CREDENTIAL_MASTER_KEY=$(python -m coldstack.cli genkey)   # else keys die with the process
uvicorn coldstack.api.main:app --reload --port 8000

# 2. UI
cd apps/web && npm install && npm run dev     # http://localhost:3000
```

Demo mode is on by default, so the whole path works before you paste a single key.
Turn it off and the UI asks for an Apollo key (search) and optionally a Hunter key
(email finding + verification). Keys are sealed by the vault on arrival; the browser
drops its copy as soon as the server confirms, and the API only ever returns a hint.

**Preview is free.** It runs search only and tells you what enriching that audience
would cost before you spend anything. `budget_usd` is a hard stop.

## Sending transports
Eleven transports in two families, because they are not interchangeable:

- **Mailbox** (SMTP today; Gmail OAuth and Microsoft Graph in P2) — real mailboxes,
  ~40 sends/day each after ramp, IMAP replies, genuine threading. **The only family
  permitted for cold outreach.**
- **ESP** (Resend, Mailjet, Mailchimp Transactional, SendGrid, Postmark, Brevo,
  Mailgun, MailerSend, SMTP2GO, SparkPost) — bulk relay, high volume, webhook events.
  Every one prohibits unsolicited mail in its acceptable-use policy and enforces by
  account termination, so they are available for **opt-in, warm and transactional**
  campaigns only.

A campaign declares its class (`cold | warm | opt_in | transactional`) and only
compatible senders can be bound to it. Enforced in `sending/policy.py` and again by a
Postgres trigger, because an application bug here costs the user their sending account.
See `docs/04-sending-engine.md` for the full matrix.

## Design commitments
- **BYOK only.** ColdStack never ships or resells contact data. Keys are envelope-encrypted
  per workspace and never returned to the client after write.
- **Every call is priced.** The usage ledger records cost and hit rate for each provider
  call, which is what makes the waterfall — and honest reporting — possible.
- **Tracking off by default.** Open pixels and wrapped links hurt cold-email deliverability.
  Opt in, with a warning.
- **Compliance is not a setting.** Global suppression, RFC 8058 one-click unsubscribe, and
  data-deletion jobs are core tables, not features.

## Licence
**AGPL-3.0** — see `LICENSE`.

Chosen deliberately: the obvious failure mode for a project like this is someone
wrapping it as a hosted service and closing the source. AGPL means a network-facing
fork has to publish its changes too. Self-hosting it for your own use, including
commercially, carries no such obligation.

Note that Reacher (the self-hosted verifier) is AGPL as well, so the licences agree.
