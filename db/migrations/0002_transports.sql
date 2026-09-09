-- Multi-transport sending: mailbox family (cold-safe) + ESP family (opt-in only).

-- A campaign now declares intent. This is what gates which transports it may bind.
ALTER TABLE campaign
  ADD COLUMN campaign_class text NOT NULL DEFAULT 'cold'
    CHECK (campaign_class IN ('cold','warm','opt_in','transactional'));

-- A sender is either a mailbox or an ESP connection. Both live here so a campaign
-- binds "senders", not two different concepts.
CREATE TABLE sender (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  transport     text NOT NULL,          -- 'smtp' | 'gmail_oauth' | 'ms_graph' | 'resend' | ...
  family        text NOT NULL CHECK (family IN ('mailbox','esp')),
  cold_safe     boolean NOT NULL,       -- denormalised from transport caps for the policy trigger
  label         text NOT NULL,
  from_email    citext NOT NULL,
  from_name     text,
  reply_to      citext,                 -- ESPs have no inbox: point this at a real mailbox
  domain_id     uuid REFERENCES sending_domain(id) ON DELETE SET NULL,
  mailbox_id    uuid REFERENCES mailbox(id) ON DELETE CASCADE,   -- set for family='mailbox'
  credential_id uuid REFERENCES credential(id) ON DELETE SET NULL,
  config        jsonb NOT NULL DEFAULT '{}',   -- e.g. mailgun {domain, region_host}
  daily_cap     integer NOT NULL DEFAULT 40,
  status        text NOT NULL DEFAULT 'untested'
                CHECK (status IN ('untested','ok','failing','disabled')),
  last_tested_at timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, transport, from_email)
);
CREATE INDEX ON sender (workspace_id, family);

CREATE TABLE campaign_sender (
  campaign_id   uuid REFERENCES campaign(id) ON DELETE CASCADE,
  sender_id     uuid REFERENCES sender(id) ON DELETE CASCADE,
  weight        real NOT NULL DEFAULT 1.0,
  PRIMARY KEY (campaign_id, sender_id)
);

-- The policy gate, enforced in the database as well as in Python. Application bugs
-- should not be able to route a cold sequence through an ESP - that is an account
-- termination and a burned shared IP pool, not a recoverable error.
CREATE OR REPLACE FUNCTION assert_sender_allowed() RETURNS trigger AS $$
DECLARE
  c_class text;
  s_cold  boolean;
  s_name  text;
BEGIN
  SELECT campaign_class INTO c_class FROM campaign WHERE id = NEW.campaign_id;
  SELECT cold_safe, transport INTO s_cold, s_name FROM sender WHERE id = NEW.sender_id;
  IF c_class IN ('cold','warm') AND NOT s_cold THEN
    RAISE EXCEPTION
      'transport % cannot be bound to a % campaign: bulk ESPs prohibit unsolicited outreach in their AUP. Use a mailbox sender, or reclassify the campaign as opt_in.',
      s_name, c_class;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_assert_sender_allowed
  BEFORE INSERT OR UPDATE ON campaign_sender
  FOR EACH ROW EXECUTE FUNCTION assert_sender_allowed();

-- ESP delivery events arrive by webhook rather than IMAP.
CREATE TABLE esp_event (
  id            bigserial PRIMARY KEY,
  workspace_id  uuid NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  sender_id     uuid REFERENCES sender(id) ON DELETE SET NULL,
  provider      text NOT NULL,
  provider_message_id text,
  event         text NOT NULL,     -- delivered|bounce|complaint|open|click|unsubscribe|inbound
  recipient     citext,
  payload       jsonb NOT NULL,
  occurred_at   timestamptz,
  received_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON esp_event (workspace_id, provider, event, received_at DESC);
CREATE INDEX ON esp_event (provider_message_id);

ALTER TABLE message
  ADD COLUMN sender_id uuid REFERENCES sender(id) ON DELETE SET NULL,
  ADD COLUMN provider_message_id text;
CREATE INDEX ON message (provider_message_id);
