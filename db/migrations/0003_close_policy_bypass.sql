-- Close the cold-campaign / ESP bypass.
--
-- 0002 put the guard on campaign_sender only, which covers "bind an ESP to a cold
-- campaign" but not the two ways to reach the same state from the other direction:
--
--   1. bind an ESP to an opt_in campaign, then reclassify that campaign to cold
--   2. bind a mailbox sender, then flip that sender's cold_safe to false
--
-- Both were silently accepted, leaving a cold campaign wired to a bulk ESP - the exact
-- state the guard exists to prevent, and the one that costs the user their account.
-- State-based invariants need guarding on every table that can change the state, not
-- just the one where the relationship is created.

CREATE OR REPLACE FUNCTION assert_campaign_class_change_ok() RETURNS trigger AS $$
DECLARE
  offending text;
BEGIN
  IF NEW.campaign_class IN ('cold','warm')
     AND NEW.campaign_class IS DISTINCT FROM OLD.campaign_class THEN
    SELECT string_agg(DISTINCT s.transport, ', ')
      INTO offending
      FROM campaign_sender cs
      JOIN sender s ON s.id = cs.sender_id
     WHERE cs.campaign_id = NEW.id AND NOT s.cold_safe;
    IF offending IS NOT NULL THEN
      RAISE EXCEPTION
        'cannot reclassify campaign % to %: bulk ESP sender(s) [%] are still bound. Unbind them first - their AUPs prohibit unsolicited outreach.',
        NEW.id, NEW.campaign_class, offending;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_assert_campaign_class_change
  BEFORE UPDATE OF campaign_class ON campaign
  FOR EACH ROW EXECUTE FUNCTION assert_campaign_class_change_ok();


CREATE OR REPLACE FUNCTION assert_sender_downgrade_ok() RETURNS trigger AS $$
DECLARE
  offending text;
BEGIN
  IF OLD.cold_safe AND NOT NEW.cold_safe THEN
    SELECT string_agg(DISTINCT c.name, ', ')
      INTO offending
      FROM campaign_sender cs
      JOIN campaign c ON c.id = cs.campaign_id
     WHERE cs.sender_id = NEW.id AND c.campaign_class IN ('cold','warm');
    IF offending IS NOT NULL THEN
      RAISE EXCEPTION
        'cannot mark sender % as not cold-safe while bound to cold/warm campaign(s) [%]. Unbind it first.',
        NEW.id, offending;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_assert_sender_downgrade
  BEFORE UPDATE OF cold_safe ON sender
  FOR EACH ROW EXECUTE FUNCTION assert_sender_downgrade_ok();
