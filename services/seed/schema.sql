-- raisin: system of record
CREATE TABLE IF NOT EXISTS tenants (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS donors (
  id SERIAL PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
  email TEXT NOT NULL, phone TEXT, name TEXT, UNIQUE (tenant_id, email));
CREATE TABLE IF NOT EXISTS donations (
  id BIGINT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), donor_id INT REFERENCES donors(id),
  amount NUMERIC(12,2) NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL);
-- public /donate never accepts a client-chosen id (CSO F2); ids come from this sequence, fixtures stay below 900000
CREATE SEQUENCE IF NOT EXISTS donation_id_seq START 900000;
CREATE TABLE IF NOT EXISTS transactions (
  id BIGINT PRIMARY KEY, donation_id BIGINT REFERENCES donations(id), tenant_id TEXT NOT NULL REFERENCES tenants(id),
  amount NUMERIC(12,2) NOT NULL, currency TEXT NOT NULL, result TEXT NOT NULL, decline_code TEXT, fraud_score NUMERIC(4,2));
CREATE TABLE IF NOT EXISTS ai_registry (
  kind TEXT NOT NULL, name TEXT NOT NULL, owner TEXT NOT NULL, risk_tier INT NOT NULL,
  approved BOOLEAN NOT NULL, version TEXT NOT NULL DEFAULT '1', PRIMARY KEY (kind, name));
