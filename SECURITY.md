# Security

## Reporting

Please report vulnerabilities privately via GitHub Security Advisories on this
repository rather than opening a public issue. Include a description, affected version
or commit, and a reproduction if you have one.

## What this project holds

ColdStack is BYOK: it stores **other people's third-party API keys** (Apollo, Hunter,
LLM providers) and, once mailbox transports land, OAuth refresh tokens. That makes a
self-hosted instance a credential-theft target, and the threat model is written around
it.

### Design assumption: the database will leak

Secrets are envelope-encrypted:

```
CREDENTIAL_MASTER_KEY (environment / KMS - never in the database)
  └─ wraps a per-workspace DEK        (workspace.dek_wrapped)
       └─ seals each secret           (credential.secret_sealed)
```

AES-256-GCM throughout, with additional authenticated data binding every ciphertext to
its **workspace and provider**. A row lifted from workspace A's Apollo credential into
workspace B's Hunter row fails to decrypt rather than silently authenticating as
someone else's key.

A stolen database dump is ciphertext. A stolen dump *plus* the master key is not — keep
the key out of the database host, and back it up separately: **lose it and every stored
credential becomes permanently unreadable.**

### Other properties

- Secrets are write-only from the API's perspective. `GET /credentials` returns a
  masked hint (last four characters) and a test result, never a value.
- The web client drops its copy of a pasted key as soon as the server confirms the seal.
- Containers run as a non-root user.
- `.env` is gitignored; CI fails on credential-shaped strings outside test fixtures.

## Sending safety

Two protections are enforced in the database, not only in application code, because a
bug in either would cost a user their sending account or their domain reputation:

- A campaign classed `cold` or `warm` cannot bind a bulk-ESP sender (migration 0002),
  and cannot be reclassified into that state after the fact (migration 0003).
- Circuit breakers pause a campaign on authentication failure, and quarantine a mailbox
  on a hard-bounce rate at or above 3%.

## Not in scope

ColdStack does not ship contact data and never will. It is a pipeline that uses keys
you supply, under your own agreements with those providers. Using it to send
unsolicited mail at scale may still breach the law in your jurisdiction and the terms
of your data providers — see `docs/04-sending-engine.md`.
