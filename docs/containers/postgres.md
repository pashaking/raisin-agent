# postgres

One PostgreSQL 16 instance with pgvector, three databases. Design box: **RDS / system of record** plus the vector store
for RAG plus LiteLLM's key and spend store.

| | |
|---|---|
| Image | `pgvector/pgvector:0.8.6-pg16-trixie` |
| Port | 15432 → 5432 |
| Init | `postgres/initdb/00-create-dbs.sql` creates `litellm`, `raisin`, `kb` (+ `vector` extension) |
| Volume | `pgdata` |
| Health | `pg_isready -U poc -d litellm` |
| Credentials | user `poc`, password `POSTGRES_PASSWORD` (default `poc`); one role for all three databases (open finding F4: no RLS) |

## Databases and tables

**raisin** (schema `services/seed/schema.sql`, written by `seed`, read/written by `raisin-api`):

| Table | Purpose |
|---|---|
| `tenants(id, name)` | `tenant-a`, `tenant-b`, `tenant-c`, `platform` |
| `users(email, tenant_id, role)` | the six personas; source of JWT claims |
| `donors(id serial, tenant_id, email, phone, name)` unique `(tenant_id, email)` | PII lives here and nowhere else |
| `donations(id bigint, tenant_id, donor_id, amount, currency, status, receipt_resent_at)` | fixtures 873901..873928 (A), 991201..991204 (B), 12345 + 774101..774111 (C); `receipt_resent_at` is Phase 10's one real write -- a no-op status-flip, since this POC has no outbound email capability anywhere |
| `donation_id_seq` start 900000 | ids for public `/donate` |
| `transactions(id, donation_id, tenant_id, amount, currency, result, decline_code, fraud_score, campaign_id, gateway, occurred_at, ip_address)` | one per donation in fixtures, plus the campaign-scoped set (Phase 3); `ip_address` (nullable) is only set on campaign 2's card-testing cluster -- what `block_ip_temporarily` acts on (Phase 14) |
| `ai_registry(kind, name, owner, risk_tier, action_risk, approved, version)` | source of `policy/data/registry.json`; `action_risk` (low\|medium\|high\|critical) is tool-only (null for models/prompts) and gates OPA's tool-stage autonomy decision |
| `remediation_actions(id, tenant_id, action, params jsonb, performed_by, result jsonb, created_at)` | audit log for the six controlled-action tools (Phase 14); `create_incident` additionally writes a real `incidents` row |

**kb** (`services/seed/kb_schema.sql`, written by `seed`, read by `agent-runtime`):

`chunks(id, doc_id, chunk_id, tenant_id, classification, content, embedding vector(1536))`, index on `(tenant_id, classification)`.
Ten documents, one chunk each, tenants `platform` / `tenant-a` / `tenant-b`, classifications `platform` / `tenant-internal` /
`internal`. The retrieval query is
`WHERE tenant_id = ANY(%s) AND classification = ANY(%s) ORDER BY embedding <=> %s LIMIT 4` with the arrays supplied by OPA.

`agent_memory(tenant_id, user_id, kind, session_id, key, value jsonb, updated_at)`, primary key
`(tenant_id, user_id, kind, session_id, key)` — controlled agent memory (Phase 8), read/written by `agent-runtime`'s
`memory.py`. `kind='preference'` rows (`session_id=''`) are seed-only (`fixtures.MEMORY_PREFS`), never written by the
model; `kind='task_state'` rows are the only ones `save_task_state` can write, scoped to one caller's one session. See
[agent-runtime.md](agent-runtime.md#agent-memory-memorypy-phase-8).

`approval_requests(id, tenant_id, requested_by, session_id, tool, args jsonb, risk, status, decided_by, decided_at,
result jsonb, created_at)`, `status` one of `pending`/`rejected`/`executed`/`failed` — the human approval queue
(Phase 10), read/written by `agent-runtime`'s `approvals.py`. `args` is set once, at creation, from the model's
exact tool call; nothing ever rewrites it. See
[agent-runtime.md](agent-runtime.md#human-approval-workflow-approvalspy-phase-10).

**litellm**: LiteLLM's own schema (`LiteLLM_VerificationToken` holds the virtual keys, aliases, budgets and spend).

## When it is down

`raisin-api` cannot mint tokens or serve any route (500); `litellm` fails its health check and every model call fails;
`search_kb` raises inside the runtime. Effectively the whole stack is down. Jaeger and Phoenix keep working (Jaeger is
in-memory, Phoenix has its own volume).

## Success example

Scenario 4's retrieval, from trace `6ef6e85dfbbaa3f4dab53f634f5321a8`:

```
retrieve search_kb   retrieval.filter.tenants=[tenant-a, platform]  retrieval.filter.classifications=[platform, tenant-internal]
  embeddings text-embedding-3-small
  db.query   db.system=postgresql  db.sql.table=chunks  db.rows=4
retrieval.documents = [tenant-a/poisoned-finance-procedure.md#0:0.593, tenant-a/finance-procedure.md#0:0.586, decline-codes-runbook.md#0:0.514, donor-faq.md#0:0.384]
```

Tenant B's `finance-procedure.md` was in the table and not in the result: the filter excluded it before ranking.

## Failure example

Not a database failure but the property the schema enforces: `/donate` with a client-supplied id is rejected by the API
(422), and even if it were not, `donations.id` for public posts always comes from `nextval('donation_id_seq')`, so a
client cannot collide with or overwrite fixture ids below 900000.

## Inspection

```bash
docker compose exec postgres psql -U poc -d raisin  -c 'select id, tenant_id, status from donations order by id;'
docker compose exec postgres psql -U poc -d kb      -c 'select doc_id, tenant_id, classification from chunks;'
docker compose exec postgres psql -U poc -d litellm -c 'select key_alias, spend, max_budget from "LiteLLM_VerificationToken";'
```

## Related

- [seed.md](seed.md) (writes schema and fixtures), [raisin-api.md](raisin-api.md), [litellm.md](litellm.md)
