-- Migration 018: Restricted role for tenant-scoped work, so row-level security is enforced
--
-- Superusers and roles with BYPASSRLS ignore row-level security, even with FORCE ROW
-- LEVEL SECURITY. The official Postgres image makes POSTGRES_USER a superuser, so under
-- the default Docker setup tenant isolation relied only on the app's own WHERE clauses.
-- project_transaction() switches to this role (SET LOCAL ROLE) when the connected user
-- is a superuser or bypasses RLS; system jobs that scan all projects keep the owner role.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'changewatch_tenant') THEN
    CREATE ROLE changewatch_tenant NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
EXCEPTION WHEN insufficient_privilege THEN
  -- Without CREATEROLE the app keeps its own role; FORCE ROW LEVEL SECURITY still applies
  -- to a non-superuser table owner, so isolation holds unless that role bypasses RLS.
  RAISE NOTICE 'Cannot create role changewatch_tenant: %', SQLERRM;
END $$;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'changewatch_tenant') THEN
    IF NOT pg_has_role(current_user, 'changewatch_tenant', 'MEMBER') THEN
      EXECUTE format('GRANT changewatch_tenant TO %I', current_user);
    END IF;
    GRANT USAGE ON SCHEMA public TO changewatch_tenant;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO changewatch_tenant;
    GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO changewatch_tenant;
    -- Tables and partitions created later by this owner get the same privileges
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO changewatch_tenant;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO changewatch_tenant;
  END IF;
END $$;
