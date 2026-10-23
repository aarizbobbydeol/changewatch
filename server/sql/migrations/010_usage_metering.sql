-- Migration 010: Usage Metering, Price Book, and Budgets
-- Specified in Usage Metering Addendum Section B

-- 1. Price Book Table (Versioned rate card)
CREATE TABLE IF NOT EXISTS price_book (
  id SERIAL PRIMARY KEY,
  version INT NOT NULL,
  meter TEXT NOT NULL,
  pricing_mode TEXT NOT NULL,                 -- 'fixed' | 'cost_plus'
  unit_price_micros BIGINT NOT NULL DEFAULT 0,
  margin_multiplier NUMERIC(6,3) NOT NULL DEFAULT 1.000,
  min_unit_price_micros BIGINT NOT NULL DEFAULT 0,
  free_units_per_month BIGINT NOT NULL DEFAULT 0,
  effective_from TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (version, meter)
);

-- Seed Version 1 starter price book
INSERT INTO price_book (version, meter, pricing_mode, unit_price_micros, margin_multiplier, min_unit_price_micros, free_units_per_month)
VALUES
  (1, 'release_monitored', 'fixed', 20000, 1.000, 0, 200),
  (1, 'ai_investigation', 'cost_plus', 0, 1.300, 1000, 20),
  (1, 'ai_investigation_deep', 'cost_plus', 0, 1.300, 5000, 0),
  (1, 'stored_events', 'fixed', 2, 1.000, 0, 100000)
ON CONFLICT (version, meter) DO NOTHING;

-- 2. Granular Usage Events Table (Immutable ledger)
CREATE TABLE IF NOT EXISTS usage_events (
  id BIGSERIAL PRIMARY KEY,
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  meter TEXT NOT NULL,
  units BIGINT NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  ref_type TEXT,
  ref_id BIGINT,
  our_cost_micros BIGINT NOT NULL DEFAULT 0,
  billed_micros BIGINT NOT NULL DEFAULT 0,
  free_units_applied BIGINT NOT NULL DEFAULT 0,
  price_book_version INT NOT NULL,
  idempotency_key TEXT NOT NULL,
  UNIQUE (project_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS usage_project_time ON usage_events (project_id, occurred_at);

-- 3. Daily Usage Rollup Table
CREATE TABLE IF NOT EXISTS usage_daily (
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  day DATE NOT NULL,
  meter TEXT NOT NULL,
  units BIGINT NOT NULL DEFAULT 0,
  our_cost_micros BIGINT NOT NULL DEFAULT 0,
  billed_micros BIGINT NOT NULL DEFAULT 0,
  free_units_applied BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (project_id, day, meter)
);

-- 4. Budgets Table (Per-project limits)
CREATE TABLE IF NOT EXISTS budgets (
  project_id INT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  soft_limit_micros BIGINT,                   -- Warn only
  hard_limit_micros BIGINT,                   -- Pause optional extras
  warn_at JSONB NOT NULL DEFAULT '[50,80,100]'
);

-- 5. Monthly Budget State Table
CREATE TABLE IF NOT EXISTS budget_state (
  project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  month DATE NOT NULL,
  spent_micros BIGINT NOT NULL DEFAULT 0,
  last_warned_pct INT NOT NULL DEFAULT 0,
  hard_limit_hit_at TIMESTAMPTZ,
  PRIMARY KEY (project_id, month)
);

-- 6. Add pricing version and price lock to projects
ALTER TABLE projects
  ADD COLUMN IF NOT EXISTS price_book_version INT,
  ADD COLUMN IF NOT EXISTS price_lock_until TIMESTAMPTZ;

-- 7. Enable and enforce Row-Level Security
ALTER TABLE usage_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS usage_events_project_policy ON usage_events;
CREATE POLICY usage_events_project_policy ON usage_events
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

ALTER TABLE usage_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_daily FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS usage_daily_project_policy ON usage_daily;
CREATE POLICY usage_daily_project_policy ON usage_daily
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

ALTER TABLE budgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE budgets FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS budgets_project_policy ON budgets;
CREATE POLICY budgets_project_policy ON budgets
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);

ALTER TABLE budget_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE budget_state FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS budget_state_project_policy ON budget_state;
CREATE POLICY budget_state_project_policy ON budget_state
  FOR ALL
  USING (project_id = NULLIF(current_setting('app.project_id', true), '')::int)
  WITH CHECK (project_id = NULLIF(current_setting('app.project_id', true), '')::int);
