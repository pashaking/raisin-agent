-- raisin: system of record
CREATE TABLE IF NOT EXISTS tenants (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS donors (
  id SERIAL PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
  email TEXT NOT NULL, phone TEXT, name TEXT, UNIQUE (tenant_id, email));
CREATE TABLE IF NOT EXISTS donations (
  id BIGINT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), donor_id INT REFERENCES donors(id),
  amount NUMERIC(12,2) NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL, receipt_resent_at TIMESTAMPTZ);
-- migration for a table created before receipt_resent_at existed (Phase 10: resend_receipt is a no-op status-flip --
-- this POC has no outbound email capability at all, see docker-compose.yml)
ALTER TABLE donations ADD COLUMN IF NOT EXISTS receipt_resent_at TIMESTAMPTZ;
-- public /donate never accepts a client-chosen id (CSO F2); ids come from this sequence, fixtures stay below 900000
CREATE SEQUENCE IF NOT EXISTS donation_id_seq START 900000;
CREATE TABLE IF NOT EXISTS transactions (
  id BIGINT PRIMARY KEY, donation_id BIGINT REFERENCES donations(id), tenant_id TEXT NOT NULL REFERENCES tenants(id),
  amount NUMERIC(12,2) NOT NULL, currency TEXT NOT NULL, result TEXT NOT NULL, decline_code TEXT, fraud_score NUMERIC(4,2));
-- Transaction-investigation tool set (plan Phase 3): campaign linkage, gateway and occurrence time. Legacy fixture
-- rows (above) get the column defaults; only the campaign-scoped synthetic fixtures below set these explicitly.
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS campaign_id INT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS gateway TEXT NOT NULL DEFAULT 'stripe';
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS occurred_at TIMESTAMPTZ NOT NULL DEFAULT now();
-- Controlled actions (plan Phase 14): the one piece of card-testing evidence block_ip_temporarily needs that the
-- Phase 3 investigation tool set didn't carry -- a source IP. NULL on every pre-existing row; only the campaign-2
-- card-testing cluster fixture sets it (services/seed/fixtures.py CAMPAIGN_TRANSACTIONS).
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS ip_address TEXT;

-- campaign_id above has no FK constraint: Postgres has no "ADD CONSTRAINT IF NOT EXISTS", and this table must exist
-- before the constraint could be added anyway (CREATE TABLE IF NOT EXISTS runs after transactions, first time through).
CREATE TABLE IF NOT EXISTS campaigns (
  id SERIAL PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
  name TEXT NOT NULL, status TEXT NOT NULL, goal_amount NUMERIC(12,2) NOT NULL);

CREATE TABLE IF NOT EXISTS application_errors (
  id SERIAL PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
  service TEXT NOT NULL, error_type TEXT NOT NULL, message TEXT NOT NULL, occurred_at TIMESTAMPTZ NOT NULL);

CREATE TABLE IF NOT EXISTS incidents (
  id SERIAL PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
  title TEXT NOT NULL, status TEXT NOT NULL, severity TEXT NOT NULL, summary TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL, resolved_at TIMESTAMPTZ);
-- risk_tier: generic governance tier (owner/version bookkeeping), set on models, tools and prompts alike.
-- action_risk: tool-only autonomy gate (low|medium|high|critical) OPA enforces at the tool stage; null for
-- models/prompts, which have no action to gate.
CREATE TABLE IF NOT EXISTS ai_registry (
  kind TEXT NOT NULL, name TEXT NOT NULL, owner TEXT NOT NULL, risk_tier INT NOT NULL, action_risk TEXT,
  approved BOOLEAN NOT NULL, version TEXT NOT NULL DEFAULT '1', PRIMARY KEY (kind, name));
-- migration for a table created before action_risk existed (CREATE TABLE IF NOT EXISTS above is a no-op on a pre-existing one)
ALTER TABLE ai_registry ADD COLUMN IF NOT EXISTS action_risk TEXT;

-- Controlled actions (plan Phase 14): audit trail for the six action_risk=low/medium remediation tools, which OPA's
-- tool stage auto-executes (unlike resend_receipt/high, no approval_requests row exists for these). One row per
-- executed call, written by the handler itself right before it returns -- not an approval queue, just a record of
-- what ran, for whom, with what result.
CREATE TABLE IF NOT EXISTS remediation_actions (
  id SERIAL PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), action TEXT NOT NULL,
  params JSONB NOT NULL, performed_by TEXT NOT NULL, result JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
