# ColdStack — architecture

> Working name. Open source, self-hostable, BYOK. Nothing proprietary in the data layer.

## Principle
ListKit sells you a database. ColdStack sells you a **pipeline** that can point at any
database you have keys for. We own the schema, the resolution logic, the sending engine
and the deliverability instrumentation — the parts that are actually hard and that no
vendor will give you.

## Layer map

```
L7  AI            ICP -> query compiler | copy agent | personalisation | reply classifier   (BYOK LLM)
L6  Inbox         IMAP sync, threading, reply/bounce/OOO detection, suppression
L5  Campaigns     sequence builder, scheduler, mailbox rotation, throttle, A/B, circuit breakers
L4  Infra         domains, DNS doctor (SPF/DKIM/DMARC/MX/blacklist), mailbox pool, warmup
L3  Verification  syntax -> MX -> SMTP RCPT -> catch-all -> risk score   (Reacher / BYOK)
L2  Enrichment    waterfall cascade with per-provider cost + hit-rate ledger
L1  Audience      provider adapters -> normalised person/company -> identity resolution -> lists
L0  Platform      workspaces, RBAC, encrypted BYOK credential vault, usage ledger, audit log
```

Every layer is callable on its own. Someone who only wants the verifier, or only the
sending engine, should be able to run just that.

## Stack

| Concern | Choice | Why |
|---|---|---|
| Web app | Next.js 15 (App Router) + TypeScript + shadcn/ui + Tailwind | fast UI, good table/filter primitives |
| API | Next.js route handlers for CRUD; FastAPI for pipeline endpoints | keep heavy work out of the edge runtime |
| Workers | Python 3.11 + Celery + Redis (Temporal if sequences outgrow it) | the enrichment/verify/send ecosystem is Python-native |
| DB | Postgres 16 + `pgvector` + `pg_trgm` + `citext` | vector ICP matching and fuzzy dedupe in one engine |
| Search | Postgres FTS first, Typesense later if list sizes demand it | avoid premature infra |
| Auth | Better Auth (or NextAuth) + workspace RBAC | self-host friendly, no vendor |
| Queue UI | Flower / Celery events | observability from day one |
| Secrets | libsodium sealed box, envelope encryption per workspace | see below |

### Why the split stack
Next.js owns anything a user watches happen. Python owns anything that runs for minutes
to weeks: enrichment waterfalls, SMTP probes, warmup schedules, multi-week sequences.
The boundary is a job table in Postgres plus Redis as the broker — no RPC coupling.

## BYOK credential vault
Per-workspace data-encryption key (DEK), itself encrypted by a master key from the
environment (`CREDENTIAL_MASTER_KEY`) or a KMS. Provider keys are sealed with the DEK
and **never** returned to the client after write — the UI only ever sees a masked hint,
the last four characters, and a live "test connection" result. Every use of a key writes
an audit row: workspace, provider, endpoint, records touched, cost, latency.

Rationale: an open-source tool that stores third-party API keys is a credential-theft
target. Assume the database will be dumped and design so that a dump is not a breach.

## Cost ledger (the differentiating primitive)
Every provider call records `provider, operation, units, unit_cost, hit (bool), latency_ms`.
This drives three things ListKit cannot offer:
- **Waterfall ordering** — automatically reorder the cascade by observed cost-per-hit
  for *your* ICP, not the vendor's averages.
- **Budget guardrails** — hard stop a list build at $X.
- **Honest reporting** — "this 4,000-lead list cost $61.20 and 78% of it came from the
  cheapest provider in the chain."

## Deliverability instrument panel
Not a toggle. A live panel per sending domain: SPF/DKIM/DMARC/MX record validity, DMARC
policy strength, Spamhaus/DBL DNS lookups, tracking-domain isolation check, per-mailbox
ramp position, rolling bounce rate, rolling spam-complaint proxy, reply rate. Campaigns
auto-pause when the rolling bounce rate crosses ~3% or reply-rate collapses — the failure
mode that quietly burns a domain.

Default: **link and open tracking OFF**. Tracking pixels and wrapped links are among the
strongest spam signals in cold email. Make the user opt in, with a warning.

## Repo layout
```
apps/web              Next.js app
services/worker       FastAPI + Celery (coldstack package)
  coldstack/providers  adapter interface + one module per vendor
  coldstack/enrich     waterfall orchestration + cost ledger
  coldstack/verify     Reacher client, BYOK verifier adapters, native checks
  coldstack/sending    Gmail/Graph/SMTP transports, scheduler, warmup
  coldstack/signals    derived intent (hiring, tech stack, funding, GitHub)
  coldstack/ai         ICP->query, copy agent, reply classifier
packages/schema       shared TS types generated from the SQL schema
db/migrations         plain SQL, runs on container init
infra                 deploy recipes (compose, Helm later)
docs                  this
```
