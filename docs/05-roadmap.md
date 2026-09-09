# Roadmap

Phases are vertical slices — each one ends with something runnable, not a half-built layer.

## P0 — Skeleton + first vertical slice (2 weeks)
Monorepo, auth, workspaces, encrypted BYOK vault, Postgres schema, provider adapter
interface, **two** adapters (Apollo search + Hunter email-find), a filter UI, CSV export.
**Done when:** paste an Apollo key → filter → 500 leads with emails → download CSV.

## P1 — Enrichment + verification (2 weeks)
Waterfall orchestrator with cost ledger, 3–4 more email-finder adapters, Reacher
self-host client, one BYOK verifier fallback, catch-all policy, lists, saved searches,
dedupe + global suppression.
**Done when:** the same list costs measurably less and every row has a verification verdict.

## P2 — Sending (3 weeks)
Mailbox connection (Gmail OAuth, SMTP/IMAP), DNS doctor, warmup pool, ramp enforcement,
sequence builder, scheduler, rotation, throttling, A/B variants.
**Done when:** a 3-step sequence sends from 3 warmed mailboxes on a real domain.

## P3 — Inbox + analytics (2 weeks)
IMAP sync, threading, reply/bounce/OOO detection, auto-stop on reply, master inbox UI,
circuit breakers, per-campaign and per-mailbox analytics.
**Done when:** replies land in one place and a bad campaign pauses itself.

## P4 — AI + signals (2–3 weeks)
ICP text → structured query, copy agent (3-step + variants), per-lead personalisation with
source citations, reply classifier, derived signal engine (hiring, tech stack, funding,
job change, GitHub).
**Done when:** plain-English ICP produces a scored list with explainable signals.

## P5 — Open-source release (2 weeks)
One-command docker compose, Helm chart, docs site, seed data, adapter-authoring guide,
security policy, licence decision.
**Done when:** a stranger self-hosts it in under 30 minutes.

## Licence
**AGPL-3.0** for the server. It keeps a SaaS fork from closing the source, which is exactly
the failure mode here — the commercial incentive to wrap this and resell it is obvious.
Adapters and the schema package under Apache-2.0 so third parties can write connectors
without licence friction. (Note: Reacher is AGPL, which pushes the same way.)

## Realistic risks
1. **Provider ToS.** Apollo and PDL restrict redistribution. Self-hosted, own-use is fine;
   a hosted multi-tenant version using your keys for customers is not. Keep BYOK strict.
2. **Deliverability is a moving target.** Google and Microsoft bulk-sender rules tightened
   through 2024–2026. The DNS doctor and circuit breakers need maintenance, not just a build.
3. **Port 25.** Reacher needs it; most clouds block it. Document the VPS requirement early.
4. **Scope.** The sending engine alone is a product. Resist building all eight modules at
   50% quality — P0–P1 done well already beats paying $597/mo.
