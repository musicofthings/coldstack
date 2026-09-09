-- ColdStack initial schema (Postgres 16)
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------- L0 platform ----------
CREATE TABLE workspace (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name          text NOT NULL,
  dek_wrapped   bytea NOT NULL,              -- workspace DEK, sealed by CREDENTIAL_MASTER_KEY
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app_user (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email         citext UNIQUE NOT NULL,
  name          text,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE membership (
  workspace_id  uuid REFERENCES workspace(id) ON DELETE CASCADE,
  user_id       uuid REFERENCES app_user(id) ON DELETE CASCADE,
  role          text NOT NULL CHECK (role IN ('owner','admin','member','viewer')),
  PRIMARY KEY (workspace_id, user_id)
);

-- BYOK vault. Ciphertext only; never returned to the client after write.
CREATE TABLE credential (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  provider      text NOT NULL,               -- 'apollo' | 'hunter' | 'anthropic' | ...
  label         text,
  secret_sealed bytea NOT NULL,
  hint          text,                        -- last 4 chars, for the UI
  status        text NOT NULL DEFAULT 'untested' CHECK (status IN ('untested','ok','failing','revoked')),
  last_tested_at timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, provider, label)
);

-- Every provider call. Drives waterfall ordering, budgets and honest cost reporting.
CREATE TABLE usage_ledger (
  id            bigserial PRIMARY KEY,
  workspace_id  uuid NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  provider      text NOT NULL,
  operation     text NOT NULL,               -- 'search' | 'find_email' | 'verify' | 'enrich' | 'llm'
  units         integer NOT NULL DEFAULT 1,
  unit_cost_usd numeric(10,6) NOT NULL DEFAULT 0,
  hit           boolean,
  latency_ms    integer,
  job_id        uuid,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON usage_ledger (workspace_id, provider, created_at DESC);

-- ---------- L1 audience ----------
CREATE TABLE company (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  domain        citext,
  name          text,
  legal_id      text,                        -- OpenCorporates / LEI / CIN — canonical dedupe key
  linkedin_url  text,
  industry      text,
  headcount     integer,
  headcount_band text,
  revenue_band  text,
  country       text, region text, city text,
  tech_stack    text[],
  founded_year  integer,
  description   text,
  embedding     vector(1536),                -- semantic ICP matching
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ON company (workspace_id, domain) WHERE domain IS NOT NULL;
CREATE INDEX ON company USING gin (name gin_trgm_ops);

CREATE TABLE person (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  first_name    text, last_name  text, full_name text,
  linkedin_url  text,
  country       text, city text, timezone text,
  embedding     vector(1536),
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ON person (workspace_id, linkedin_url) WHERE linkedin_url IS NOT NULL;
CREATE INDEX ON person USING gin (full_name gin_trgm_ops);

-- person-in-a-role. Separating this is what makes job-change signals possible.
CREATE TABLE contact (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  person_id     uuid NOT NULL REFERENCES person(id) ON DELETE CASCADE,
  company_id    uuid REFERENCES company(id) ON DELETE SET NULL,
  title         text,
  seniority     text,                        -- c_suite|vp|director|manager|ic|founder|owner
  department    text,
  email         citext,
  email_status  text DEFAULT 'unknown'
                CHECK (email_status IN ('unknown','valid','invalid','catch_all','risky','unverified')),
  email_verified_at timestamptz,
  email_source  text,
  phone         text,
  started_at    date, ended_at date,
  is_current    boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ON contact (workspace_id, email) WHERE email IS NOT NULL;
CREATE INDEX ON contact (workspace_id, company_id);

-- Raw vendor payloads, immutable. Normalised rows are a projection over these.
CREATE TABLE provider_record (
  id            bigserial PRIMARY KEY,
  workspace_id  uuid NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  provider      text NOT NULL,
  entity_type   text NOT NULL CHECK (entity_type IN ('person','company','email','signal')),
  external_id   text,
  payload       jsonb NOT NULL,
  person_id     uuid REFERENCES person(id) ON DELETE SET NULL,
  company_id    uuid REFERENCES company(id) ON DELETE SET NULL,
  fetched_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON provider_record (workspace_id, provider, entity_type);
CREATE INDEX ON provider_record USING gin (payload jsonb_path_ops);

CREATE TABLE identity_edge (
  id            bigserial PRIMARY KEY,
  workspace_id  uuid NOT NULL,
  entity_type   text NOT NULL CHECK (entity_type IN ('person','company')),
  left_id       uuid NOT NULL, right_id uuid NOT NULL,
  score         real NOT NULL,
  rule          text NOT NULL,               -- 'exact_email' | 'linkedin' | 'domain+name' | 'trgm'
  decision      text NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending','merged','rejected')),
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE list (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  name          text NOT NULL,
  query         jsonb,                       -- the compiled ICP query, re-runnable
  icp_text      text,                        -- the original plain-English description
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE list_member (
  list_id       uuid REFERENCES list(id) ON DELETE CASCADE,
  contact_id    uuid REFERENCES contact(id) ON DELETE CASCADE,
  added_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (list_id, contact_id)
);

-- ---------- L3 verification cache ----------
CREATE TABLE verification (
  email         citext PRIMARY KEY,
  verdict       text NOT NULL CHECK (verdict IN ('valid','invalid','catch_all','risky','unknown')),
  is_disposable boolean, is_role_account boolean, mx_found boolean, smtp_accepts boolean,
  provider      text NOT NULL,
  raw           jsonb,
  checked_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------- L4 infrastructure ----------
CREATE TABLE sending_domain (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  domain        citext NOT NULL,
  registrar     text, dns_provider text,
  spf_ok boolean, dkim_ok boolean, dmarc_ok boolean, mx_ok boolean,
  dmarc_policy  text, blacklist_hits text[],
  tracking_domain text,
  last_checked_at timestamptz,
  UNIQUE (workspace_id, domain)
);

CREATE TABLE mailbox (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  domain_id     uuid REFERENCES sending_domain(id) ON DELETE SET NULL,
  email         citext NOT NULL,
  display_name  text,
  transport     text NOT NULL CHECK (transport IN ('gmail_oauth','ms_graph','smtp')),
  credential_id uuid REFERENCES credential(id) ON DELETE SET NULL,
  daily_cap     integer NOT NULL DEFAULT 40,
  ramp_started_at date,
  ramp_day      integer NOT NULL DEFAULT 0,
  status        text NOT NULL DEFAULT 'warming'
                CHECK (status IN ('warming','healthy','throttled','quarantined','disabled')),
  sent_today    integer NOT NULL DEFAULT 0,
  bounce_rate_7d real,
  reply_rate_7d  real,
  UNIQUE (workspace_id, email)
);

-- ---------- L5 campaigns ----------
CREATE TABLE campaign (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  name          text NOT NULL,
  status        text NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft','running','paused','auto_paused','completed')),
  pause_reason  text,
  timezone_mode text NOT NULL DEFAULT 'recipient',
  send_window   int4range NOT NULL DEFAULT int4range(9,17),
  send_days     int[] NOT NULL DEFAULT '{1,2,3,4,5}',
  track_opens   boolean NOT NULL DEFAULT false,   -- off by default, on purpose
  track_clicks  boolean NOT NULL DEFAULT false,
  daily_limit   integer NOT NULL DEFAULT 200,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE campaign_mailbox (
  campaign_id   uuid REFERENCES campaign(id) ON DELETE CASCADE,
  mailbox_id    uuid REFERENCES mailbox(id) ON DELETE CASCADE,
  PRIMARY KEY (campaign_id, mailbox_id)
);

CREATE TABLE sequence_step (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id   uuid NOT NULL REFERENCES campaign(id) ON DELETE CASCADE,
  step_index    integer NOT NULL,
  delay_days    integer NOT NULL DEFAULT 3,
  same_thread   boolean NOT NULL DEFAULT true,
  UNIQUE (campaign_id, step_index)
);
CREATE TABLE step_variant (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  step_id       uuid NOT NULL REFERENCES sequence_step(id) ON DELETE CASCADE,
  label         text NOT NULL DEFAULT 'A',
  subject       text, body text NOT NULL,
  weight        real NOT NULL DEFAULT 1.0
);

CREATE TABLE enrollment (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id   uuid NOT NULL REFERENCES campaign(id) ON DELETE CASCADE,
  contact_id    uuid NOT NULL REFERENCES contact(id) ON DELETE CASCADE,
  status        text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','replied','bounced','unsubscribed','completed','suppressed')),
  current_step  integer NOT NULL DEFAULT 0,
  next_send_at  timestamptz,
  thread_id     text,
  UNIQUE (campaign_id, contact_id)
);
CREATE INDEX ON enrollment (next_send_at) WHERE status = 'active';

CREATE TABLE message (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL,
  enrollment_id uuid REFERENCES enrollment(id) ON DELETE CASCADE,
  mailbox_id    uuid REFERENCES mailbox(id) ON DELETE SET NULL,
  variant_id    uuid REFERENCES step_variant(id) ON DELETE SET NULL,
  direction     text NOT NULL CHECK (direction IN ('outbound','inbound')),
  message_id_hdr text, in_reply_to text,
  subject       text, body text,
  sent_at       timestamptz, received_at timestamptz,
  classification text,   -- positive|neutral|objection|referral|unsubscribe|auto_reply|ooo|bounce
  bounce_type   text,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON message (workspace_id, message_id_hdr);

-- ---------- Suppression: checked at send time, never optional ----------
CREATE TABLE suppression (
  id            bigserial PRIMARY KEY,
  workspace_id  uuid NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  email         citext, domain citext,
  reason        text NOT NULL CHECK (reason IN ('unsubscribe','hard_bounce','complaint','competitor','customer','manual','dnc')),
  source        text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (email IS NOT NULL OR domain IS NOT NULL)
);
CREATE INDEX ON suppression (workspace_id, email);
CREATE INDEX ON suppression (workspace_id, domain);

-- ---------- Signals ----------
CREATE TABLE signal (
  id            bigserial PRIMARY KEY,
  workspace_id  uuid NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  company_id    uuid REFERENCES company(id) ON DELETE CASCADE,
  person_id     uuid REFERENCES person(id) ON DELETE CASCADE,
  kind          text NOT NULL,     -- hiring|tech_change|funding|job_change|github|news|web_change
  score         real NOT NULL DEFAULT 0,
  summary       text NOT NULL,
  source_url    text,              -- explainability: every signal cites its source
  observed_at   timestamptz NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON signal (workspace_id, kind, observed_at DESC);
