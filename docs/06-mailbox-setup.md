# Connecting mailboxes: Google Workspace and Microsoft 365

Two console setups, ~15 minutes each. Neither requires app verification, a security
assessment, or a published OAuth consent screen — provided you take the paths below.

**Do not paste any of the credentials produced here into a chat, an issue, or a commit.**
They go into the running app's credential vault, or into `.env`, which is gitignored.

---

## Google Workspace — service account with domain-wide delegation

### Why this path and not "Sign in with Google"

The obvious approach — an OAuth consent screen and a per-mailbox authorisation flow —
is the wrong one for a sending tool, for three reasons:

1. Gmail's read scopes are **restricted**. Any app that requests them, has a published
   consent screen, and can move that data to a third-party server must pass a
   [CASA security assessment](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)
   by a Google-empanelled assessor, renewed annually. That is weeks and real money.
2. Staying in **Testing** publishing status avoids verification, but refresh tokens
   issued by an unverified app expire after seven days. A sending tool that silently
   stops every week is not a tool.
3. Every mailbox you add needs a human to click through a consent screen.

A **service account with domain-wide delegation** sidesteps all three. Your Workspace
admin authorises the service account once, for named scopes, and it can then impersonate
any mailbox on the domain. No consent screen, no verification, no CASA, no expiring
refresh tokens. Google documents it as a
[supported administrative control](https://knowledge.workspace.google.com/admin/apps/control-api-access-with-domain-wide-delegation),
not a workaround.

This is also why it stays viable if ColdStack is ever hosted for other organisations:
each customer's admin performs their own delegation grant. The verification burden never
lands on the app.

### Steps

**In Google Cloud Console** (`console.cloud.google.com`)

1. Create a project — e.g. `coldstack-mail`.
2. **APIs & Services → Library** → enable the **Gmail API**.
3. **IAM & Admin → Service Accounts → Create service account**.
   Name it `coldstack-sender`. Grant it **no project roles** — it needs none; its power
   comes from the delegation grant, not from IAM.
4. Open the service account → **Details** → copy the **Unique ID**. This is a long
   numeric string, and it is what the Workspace admin console calls the *Client ID*.
5. **Keys → Add key → Create new key → JSON**. Download it. This file is the credential.

**In Google Workspace Admin** (`admin.google.com`)

6. **Security → Access and data control → API controls → Manage Domain-Wide Delegation
   → Add new**.
7. **Client ID**: the numeric Unique ID from step 4.
8. **OAuth scopes** — paste as a comma-separated list, exactly these and no more:

   ```
   https://www.googleapis.com/auth/gmail.send,
   https://www.googleapis.com/auth/gmail.readonly
   ```

   Add `https://www.googleapis.com/auth/gmail.modify` **only** if you want ColdStack to
   label or mark replies read in the mailbox. It is a broader grant; skip it unless you
   want that behaviour.

9. Authorise. There is no consent screen and nothing else to approve.

### What to hand ColdStack

The JSON key file, plus the domain it may impersonate. ColdStack seals the key in the
vault and mints a token per mailbox by setting `subject` to that mailbox's address.

### Two things worth knowing

**That key can impersonate any mailbox on the domain**, within the granted scopes. Treat
it like a domain credential. Which is an argument for something you want anyway: run cold
outreach from a **separate Workspace on a separate domain** from your primary one. It
isolates the blast radius here, and it isolates sending reputation — a burned outreach
domain must not take your real mail down with it.

**Sending limits**: Workspace allows 2,000 messages/day per user via the API. ColdStack's
ramp caps a mailbox at ~40/day, so the API limit will never be the binding constraint.

---

## Microsoft 365 — Entra app with Graph application permissions

### Why Graph and not SMTP

Microsoft is retiring basic authentication for SMTP AUTH client submission. Per
Microsoft's
[updated timeline](https://techcommunity.microsoft.com/blog/exchange/updated-exchange-online-smtp-auth-basic-authentication-deprecation-timeline/4489835),
existing tenants keep it until **31 December 2026**, new tenants are blocked from
**1 January 2027**, and a final removal date for existing tenants will be announced in
**H2 2027**. Building on SMTP AUTH now buys you a migration project later. Graph is the
destination either way.

### Steps

**In Microsoft Entra admin centre** (`entra.microsoft.com`)

1. **App registrations → New registration**. Name it `ColdStack`. Single tenant. Leave
   the redirect URI blank — the client-credentials flow does not use one.
2. Copy the **Application (client) ID** and **Directory (tenant) ID**.
3. **Certificates & secrets → New client secret**. Copy the *Value* immediately; it is
   shown once. (A certificate is better for production; a secret is fine to start.)
4. **API permissions → Add a permission → Microsoft Graph → Application permissions**.
   Add `Mail.Send` and `Mail.ReadWrite`. Then **Grant admin consent**.

**Now the step most guides omit, and the one that matters most.**

`Mail.Send` as an *application* permission grants send-as rights over **every mailbox in
the tenant** — your CEO's included. It must be scoped down.

Microsoft Learn now marks Application Access Policies as **legacy**; the current
mechanism is
[RBAC for Applications](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac).

5. **Enterprise applications** → find `ColdStack` → copy its **Object ID**.
   This is *not* the Object ID shown under App registrations. Use the one from
   Enterprise applications; Microsoft's own documentation calls this out because getting
   it wrong is the usual failure.

**In Exchange Online PowerShell**

```powershell
Connect-ExchangeOnline

# 1. Tag the mailboxes ColdStack is allowed to touch.
Set-Mailbox -Identity outreach1@sending-domain.com -CustomAttribute1 "coldstack"
Set-Mailbox -Identity outreach2@sending-domain.com -CustomAttribute1 "coldstack"

# 2. A scope that resolves to exactly those mailboxes.
New-ManagementScope -Name "ColdStack senders" `
  -RecipientRestrictionFilter "CustomAttribute1 -eq 'coldstack'"

# 3. Point Exchange at the Entra service principal.
#    -ObjectId is the ENTERPRISE APPLICATION object id, not the app registration's.
New-ServicePrincipal -AppId <application-client-id> `
  -ObjectId <enterprise-app-object-id> -DisplayName "ColdStack"

# 4. Grant each role, bounded by the scope.
New-ManagementRoleAssignment -App <enterprise-app-object-id> `
  -Role "Application Mail.Send" -CustomResourceScope "ColdStack senders"
New-ManagementRoleAssignment -App <enterprise-app-object-id> `
  -Role "Application Mail.ReadWrite" -CustomResourceScope "ColdStack senders"
```

Verify by attempting a send as a mailbox *outside* the scope — it must fail with
`ErrorAccessDenied`. A setup that succeeds there is not scoped, whatever the portal shows.

### What to hand ColdStack

Tenant ID, client ID, client secret, and the list of sending mailbox addresses.

### Endpoints ColdStack will use

```
POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
     grant_type=client_credentials
     scope=https://graph.microsoft.com/.default

POST https://graph.microsoft.com/v1.0/users/{mailbox}/sendMail
```

---

## Before either: the domain has to be ready

Neither transport helps if the domain is not authenticated. Run the DNS doctor against
your sending domain first and clear every FAIL:

```bash
cd services/worker
python -c "
from coldstack.sending.dns_doctor import diagnose
print(diagnose('your-sending-domain.com').summary())"
```

SPF, DKIM and DMARC must be in place *before* the first send, not after the first bounce
report. And remember the ramp: a new mailbox starts at 5 sends/day and takes about two
weeks to reach its cap. That clock starts when you connect the mailbox, so connect them
sooner than you think you need them.
