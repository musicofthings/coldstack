# BYOK provider landscape

Every row is an adapter behind one interface (`coldstack/providers/base.py`). Prices are
list prices as of mid-2026 and drift — treat them as ordering hints, not gospel; the cost
ledger measures reality.

## People + company search (replaces the 977M database)
| Provider | Strength | Rough cost | Notes |
|---|---|---|---|
| **Apollo.io** | Broadest single source, good filters, generous API | ~$0.01–0.05/contact | The realistic default. ToS restricts redistribution — fine for own use. |
| **People Data Labs** | Bulk person/company enrichment, clean schema | ~$0.02–0.10/match | Best for enrichment, weaker for discovery. |
| **Crustdata** | Real-time company + headcount/hiring deltas | subscription | Strong for signals, not bulk lists. |
| **Coresignal** | Firmographic + employee datasets, bulk delivery | enterprise | Good if you want to own a corpus later. |
| **Exa / Serper** | Semantic and keyword web search | ~$0.001–0.005/query | The cheap discovery front-end: find *companies* from a description, then enrich. |
| **OpenCorporates / GLEIF / SEC EDGAR / Companies House** | Legal entity ground truth | free | Use for canonical company identity and dedupe keys. |

## Email finding (person → work email)
Hunter.io, Findymail, LeadMagic, Prospeo, Anymailfinder, **Dropcontact** (EU-based, GDPR
posture is the cleanest for European targets). These are the classic waterfall: run them
cheapest-first, stop on first verified hit. Typical blended cost lands at $0.01–0.04 per
found email versus $0.05+ from any single vendor.

## Verification

Six email finders are wired (Hunter, Findymail, Prospeo, LeadMagic, Dropcontact, Apollo
enrichment), which is what gives the waterfall real competition to rank. Dropcontact is
the cleanest GDPR posture and strongest on EU contacts; LeadMagic and Findymail skew
North American SaaS. Dropcontact's API is async - it returns a request_id to poll - so
it currently reports a miss on the first pass until the polling worker lands.

- **Reacher** (`check-if-email-exists`) — AGPL, self-hostable, does real SMTP RCPT probes.
  Zero marginal cost. Needs outbound port 25, which most clouds block: run it on a VPS
  that permits it (Hetzner, OVH) with a clean IP and correct PTR.
- BYOK fallbacks: ZeroBounce, MillionVerifier, Bouncer, NeverBounce (~$0.0005–0.004/email).
- Catch-all domains are the real problem. Policy: never treat catch-all as valid; route
  catch-all addresses to a low-volume, separate campaign and watch bounce rate.

## Signals — our honest replacement for "21B intent signals"
Bought intent is opaque and reviewers call ListKit's "hit-or-miss". Derive instead:
- **Hiring velocity** — job postings by role/dept over time (Indeed/LinkedIn jobs, careers pages)
- **Tech-stack change** — Wappalyzer (open source) or BuiltWith on the company domain
- **Funding / M&A** — Crunchbase, news RSS, SEC filings
- **Job changes** — new role within 90 days is the strongest single trigger in cold email
- **Engineering activity** — GitHub org commit/release cadence
- **Web presence deltas** — new pricing page, new sub-brand, site relaunch
Each becomes a scored, *explainable* signal with a source URL. "Hired 3 SDRs last month
and just switched to HubSpot" beats an intent score of 87 in every reply-rate test.

## Sending transports
| Transport | Use | Notes |
|---|---|---|
| **Gmail API (OAuth)** | Google Workspace mailboxes | Best deliverability for cold; per-mailbox limits ~2k/day, use ~30–50 |
| **Microsoft Graph** | M365 mailboxes | Similar; watch tenant throttling |
| **Generic SMTP + IMAP** | Everything else, incl. private Workspace resellers | Universal fallback |
| SES / Mailgun / Resend | **Do not use for cold outreach** | Transactional ESPs will terminate you; support them for system mail only |

## LLM (BYOK)
Anthropic, OpenAI, or a local Ollama model. Used for: ICP text → structured query, copy
generation, per-lead personalisation, and reply classification. Reply classification is
the highest-value and cheapest use — a small local model handles it fine.
