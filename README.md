# ColdStack

**An open-source, self-hosted, BYOK cold outreach stack.** Build and verify B2B lead
lists through your own provider keys, then run sequences from your own mailboxes —
without a $597/month subscription or a vendor holding your data.

> **Status: pre-alpha.** List building, verification, the sequence engine and inbound
> classification all work and are tested (191 tests). **There is no Gmail or Microsoft
> OAuth transport yet** — SMTP sends, OAuth does not. Treat this as a working core, not
> a finished product.

---

## Quickstart

```bash
git clone https://github.com/musicofthings/coldstack && cd coldstack
make setup     # writes .env with a generated CREDENTIAL_MASTER_KEY
make up        # db, redis, api, web
```

Open `http://localhost:3000`. **No account and no API key are needed** — demo mode runs
the whole pipeline against deterministic fake providers.

Or without Docker:

```bash
cd services/worker && pip install -r requirements.txt
python -m coldstack.cli build --demo --limit 200 --only-valid --out leads.csv
```

Back up the key `make setup` writes. Lose it and every stored provider credential
becomes permanently unreadable — that is deliberate, see [SECURITY.md](SECURITY.md).

`make help` lists everything else.

---

## The idea

ListKit and its competitors sell you a database. ColdStack sells you the **pipeline**,
and you point it at whatever data you have keys for.

That inversion is not a compromise — the database is the one part an open-source project
genuinely cannot clone, and the pipeline is the part no vendor will hand over. So
ColdStack owns the hard, unglamorous middle: a unified schema, cost-aware enrichment,
identity resolution, real deliverability instrumentation, and a sending engine that will
not let you burn your domain.

### Three things that fall out of it

**Every call is priced.** A usage ledger records cost, latency and hit/miss for each
provider call. That is what makes the enrichment waterfall real: providers are ordered by
*your* observed cost-per-hit, not a vendor's averages. A run reports what it actually
spent and where.

```
searched          200        emails found      161
after suppression 200        verified valid    131
total cost        $0.8805    cost/valid email  $0.0067
  hunter          140/200 hits (70.0%)  $1.0000
  apollo_enrich    21/60  hits (35.0%)  $0.6300
```

**Signals you can audit.** Instead of an opaque intent score, signals are derived from
public sources and every one carries a source URL and a decayed score. `"pushed to 20
repositories in the last 90 days"` with a link beats `intent: 87`.

**Personalisation that cannot fabricate.** The model may only reference supplied signals
and must cite them; every line is then verified in code — unsupported numbers and proper
nouns are rejected, and with no signals the model is never called. An invented compliment
is worse than none, so it is structurally impossible rather than discouraged.

---

## What works today

| Area | Status |
|---|---|
| BYOK provider adapters | Apollo (search), 6 email finders, Hunter + Reacher verification |
| Cost-aware enrichment waterfall | Ranks by observed cost-per-hit, hard budget caps |
| Credential vault | AES-256-GCM envelope encryption, per-workspace + per-provider AAD |
| Verification cache | Per-verdict TTLs; reused verdicts cost nothing |
| DNS doctor | SPF (with lookup-limit counting), DKIM, DMARC, MX, blocklist |
| Sending engine | Ramp, rotation, domain cool-off, pacing, windows, circuit breakers |
| Sequence engine | Multi-step, A/B, stop-on-reply across campaigns, send-time suppression |
| Inbound | Bounce / auto-reply / OOO / unsubscribe classification, IMAP fetch |
| AI layer | ICP → query, copy generation + linting, citation-gated personalisation |
| Web UI | Sources, filters, free preview, build progress, CSV export |
| **Gmail / Microsoft OAuth** | **Not built.** SMTP only. |
| Master inbox UI, analytics | Not built |
| Persisted lists, saved searches | Schema exists, store does not |

---

## Sending: two transport families, and the difference matters

- **Mailbox** (SMTP today; Gmail OAuth and Graph planned) — real mailboxes, ~40 sends/day
  each after ramp, IMAP replies, genuine threading. **The only family permitted for cold
  outreach.**
- **ESP** (Resend, Mailjet, Mailchimp Transactional, SendGrid, Postmark, Brevo, Mailgun,
  MailerSend, SMTP2GO, SparkPost) — bulk relay, high volume, webhook events. Every one
  prohibits unsolicited mail in its acceptable-use policy and enforces by account
  termination, so they are available for **opt-in, warm and transactional** campaigns only.

A campaign declares its class (`cold | warm | opt_in | transactional`) and only compatible
senders can bind to it. Enforced in application code *and* by a Postgres trigger, because
a bug here costs a user their sending account.

Open and click tracking are **off by default**. Both hurt cold-email placement; opt in
knowingly.

---

## How it is put together

```
L7  AI            ICP → query | copy + lint | personalisation | reply intent   (BYOK LLM)
L6  Inbox         IMAP fetch, threading, bounce/OOO/reply classification
L5  Campaigns     sequences, scheduler, rotation, throttling, circuit breakers
L4  Infra         domains, DNS doctor, mailbox pool, warmup
L3  Verification  syntax → MX → SMTP → catch-all → risk, cached by verdict TTL
L2  Enrichment    waterfall with a cost ledger
L1  Audience      provider adapters → normalised person/company → lists
L0  Platform      workspaces, encrypted BYOK vault, usage ledger
```

Next.js + TypeScript for anything a user watches happen; Python for anything that runs for
minutes to weeks. Postgres with pgvector. Scheduling, sequencing and classification are
pure functions — which is why multi-week sending behaviour is testable in milliseconds.

---

## Landing page

[`index.html`](index.html) at the repository root — one self-contained file, no build step.

Publish it with **Settings → Pages → Deploy from a branch → `main` / `/ (root)`**.

It lives at the root because branch deployment only offers the root or `/docs`, and
`/docs` holds the architecture documents, which are written to be read on GitHub rather
than served as a website. `.nojekyll` stops Pages running Jekyll over the repo; nothing
here needs it.

## Documentation

| Doc | What's in it |
|---|---|
| [`docs/00-feature-teardown.md`](docs/00-feature-teardown.md) | What the incumbent actually ships, and where it is weak |
| [`docs/01-architecture.md`](docs/01-architecture.md) | Layers, stack choices, credential vault, cost ledger |
| [`docs/02-data-model.md`](docs/02-data-model.md) | Schema decisions worth defending |
| [`docs/03-providers.md`](docs/03-providers.md) | BYOK vendor landscape with costs; derived signals |
| [`docs/04-sending-engine.md`](docs/04-sending-engine.md) | Warmup, rotation, breakers, DNS, compliance |
| [`docs/05-roadmap.md`](docs/05-roadmap.md) | Phases, licence reasoning, risks |
| [`docs/06-mailbox-setup.md`](docs/06-mailbox-setup.md) | Connecting Google Workspace and Microsoft 365 mailboxes |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | **How to write a provider adapter** — the main way to extend this |
| [`SECURITY.md`](SECURITY.md) | Threat model, the vault, how to report a vulnerability |

---

## Compliance

Suppression lists, RFC 8058 one-click unsubscribe, and per-workspace data deletion are
core tables, not optional features. ColdStack does not ship contact data and never will —
it uses keys you supply under your own agreements. Sending unsolicited mail may still
breach CAN-SPAM, GDPR, CASL or India's DPDP Act depending on where you and your recipients
are; [`docs/04-sending-engine.md`](docs/04-sending-engine.md) covers what the software
does and does not do for you.

## Licence

[AGPL-3.0](LICENSE). The obvious failure mode for a project like this is someone wrapping
it as a hosted service and closing the source; AGPL requires a network-facing fork to
publish its changes. Self-hosting for your own use, commercially or otherwise, carries no
such obligation.
