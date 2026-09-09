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
Intended: **AGPL-3.0** for the server, Apache-2.0 for the adapter interface and schema
package. Not yet applied — see `docs/05-roadmap.md`.
