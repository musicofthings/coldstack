# Contributing

The most useful contribution is a **provider adapter**. ColdStack's whole premise is
that the data layer is swappable, and that only holds if adding a vendor is a small,
obvious change.

## Getting set up

```bash
make setup                 # writes .env with a generated master key
make up                    # db, redis, api, web
make test                  # 187 python tests
make demo                  # end-to-end list build with fake providers, no keys
```

No third-party account is needed to develop or to run the test suite. Everything is
exercised against deterministic fakes.

## Writing an adapter

Adapters live in `services/worker/coldstack/providers/` and implement one of the
protocols in `providers/base.py`. Nothing above that layer may import a vendor SDK.

### An email finder

Most finders are the same shape — POST a name plus a company domain, get an address or
a miss — so they are data, not classes. Add a spec to `providers/finders.py`:

```python
ACME = FinderSpec(
    name="acme",
    url="https://api.acme.io/find",
    auth=lambda k: {"Authorization": f"Bearer {k}"},
    payload=lambda p, d: {"first": p.first_name, "last": p.last_name, "domain": d},
    extract=lambda r: (r.get("data") or {}).get("email"),
    est_unit_cost_usd=0.006,
)
```

Then add it to `SPECS`. It is registered, ranked in the waterfall, and priced in the
cost ledger automatically.

### A search provider or verifier

Subclass and decorate with `@register("search")` / `@register("verifier")`. Look at
`providers/apollo.py` and `verify/reacher.py` — both are short.

### Three rules

1. **Report cost honestly.** Every call returns a `ProviderCost` with `units`,
   `unit_cost_usd` and `hit`. The waterfall reorders providers by *observed* cost per
   hit, so a lie here makes the whole ranking wrong. If the vendor does not charge for
   a miss, report `unit_cost_usd=0.0` on a miss.
2. **Never invent a result.** A missing domain, a low-confidence match, an ambiguous
   response — return `None`. A wrong address is a bounce, and a bounce costs far more
   than the credit you saved.
3. **Do not raise into the pipeline.** Network errors and 4xx/5xx responses become a
   miss with `hit=False`, not an exception. One flaky vendor must not fail a 5,000-lead
   build.

### Testing it

Add a fake alongside the real adapter, as `providers/fake.py` does, and write tests
against the fake. Do not write tests that call a live API — they cost money, they are
flaky, and they fail for anyone without that vendor's key.

## Adding a sending transport

Two families, and the distinction is load-bearing: `mailbox` transports may be used for
cold outreach, `esp` transports may not, because every bulk ESP prohibits unsolicited
mail in its acceptable-use policy and enforces by account termination.

If you add an ESP, set `cold_outreach_safe=False`. This is not a stylistic choice —
`sending/policy.py` and a Postgres trigger both refuse the binding, and
`tests/test_policy.py` asserts it for every registered ESP.

## Code conventions

- Comments explain **why**, not what. If a line encodes a hard-won fact — a rate limit,
  a spec quirk, a failure someone hit in production — say so. Most existing comments
  are of that kind; match them.
- Scheduling, sequencing and classification logic stays **pure**: state in, decisions
  out. That is what makes multi-week sending behaviour testable in milliseconds.
- New behaviour needs a test that would have failed before the change.
- Money is formatted at the boundary and kept at full precision in accumulators.

## Before opening a PR

```bash
make test && make test-web
```

CI additionally applies every migration to a real Postgres and asserts the two ESP
policy triggers still refuse a cold binding. If you touch `db/migrations/`, expect that
job to be the one that catches you.
