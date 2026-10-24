-- Migration 022: Let system jobs read every project when the database user is not a superuser
--
-- Row-level security is FORCEd, so it also applies to the table owner. On the default Docker
-- setup the app connects as a superuser, which bypasses it, so the detector, outbox delivery,
-- retention, escalations and the watchdog (which work across projects) saw every row. On managed
-- Postgres the app user is usually not a superuser: those jobs saw no rows at all, so no verdicts
-- were computed and no alerts were delivered.
--
-- Each table gets a second permissive policy that matches when app.system_scope = 'all'. Pool
-- connections start in that scope (app/db.py); project_transaction() and
-- read_project_transaction() clear it, so tenant work still sees only its own project.
-- A table that enables row-level security in a later migration needs the same policy.
DO $$
DECLARE
  t record;
BEGIN
  FOR t IN
    SELECT c.relname
    FROM pg_class c
    WHERE c.relnamespace = 'public'::regnamespace
      AND c.relkind IN ('r', 'p')
      AND c.relrowsecurity
      AND NOT c.relispartition
  LOOP
    IF NOT EXISTS (
      SELECT 1 FROM pg_policies
      WHERE schemaname = 'public' AND tablename = t.relname AND policyname = t.relname || '_system_scope'
    ) THEN
      EXECUTE format(
        'CREATE POLICY %I ON %I USING (current_setting(''app.system_scope'', true) = ''all'') '
        'WITH CHECK (current_setting(''app.system_scope'', true) = ''all'')',
        t.relname || '_system_scope', t.relname
      );
    END IF;
  END LOOP;
END $$;
