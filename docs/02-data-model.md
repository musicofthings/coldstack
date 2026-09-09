# Data model notes

Full DDL in `db/migrations/0001_init.sql`. The design decisions worth defending:

**Person and company are separate, and `contact` is the join.** A person can hold roles
at several companies over time; a campaign targets a person *in a role*. Collapsing them
(as most CRMs do) makes job-change signals — the single highest-converting cold-email
trigger — impossible to compute.

**Provider records are kept raw and immutable.** `provider_record` stores the exact JSON
each vendor returned, timestamped. The normalised `person`/`company` rows are a *projection*
over those records. When a vendor changes its schema, or you want to re-resolve with better
logic, you replay rather than re-buy.

**Identity resolution is explicit.** `identity_edge` holds candidate matches with a score
and the rule that produced it (exact email, domain+normalised name, LinkedIn URL, fuzzy
trigram on name+company). Merges are reversible. Use `splink` for probabilistic linkage
once volume justifies it.

**Suppression is global and first-class.** One `suppression` table per workspace covering
unsubscribes, hard bounces, complaints, competitor domains, existing customers, and
do-not-contact from CRM. Checked at send time, not at list-build time — lists go stale.

**Email verification results are cached with a TTL.** Verification is the second-largest
line item after data. A verdict older than ~60 days should be treated as stale; a verdict
newer than that should never be re-purchased.

**Every mutable row carries `workspace_id`.** Row-level security policies from day one —
retrofitting multi-tenancy is how self-hosted tools leak data between customers.
