-- kb: knowledge base chunks with tenant metadata for filtered retrieval
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS chunks (
  id SERIAL PRIMARY KEY,
  doc_id TEXT NOT NULL,
  chunk_id INT NOT NULL,
  tenant_id TEXT NOT NULL,
  classification TEXT NOT NULL,
  content TEXT NOT NULL,
  embedding vector(1536) NOT NULL,
  UNIQUE (doc_id, chunk_id));
CREATE INDEX IF NOT EXISTS chunks_tenant_class ON chunks (tenant_id, classification);

-- agent_memory: controlled agent memory (Phase 8), separated by purpose. kind='preference' is long-lived and never
-- written by the model (seeded by an admin path only, read via get_user_context); kind='task_state' is session-scoped
-- and the only kind save_task_state may write. tenant_id/user_id/session_id always come from the caller's verified
-- claims and request, never from a model argument.
CREATE TABLE IF NOT EXISTS agent_memory (
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('preference', 'task_state')),
  session_id TEXT NOT NULL DEFAULT '',
  key TEXT NOT NULL,
  value JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, user_id, kind, session_id, key));
CREATE INDEX IF NOT EXISTS agent_memory_lookup ON agent_memory (tenant_id, user_id, kind, session_id);

-- conversation_turns: ordered conversation replay, scoped to (tenant_id, user_id, session_id) exactly like
-- agent_memory's task_state kind. A separate table, not an agent_memory kind, because this is an append-only log
-- replayed in insertion order, never addressed by key. /run loads it (oldest first) between the system prompt and
-- the current question, and appends the user question + final (post-guardrail) answer after a successful run.
CREATE TABLE IF NOT EXISTS conversation_turns (
  id BIGSERIAL PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  content TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS conversation_turns_lookup ON conversation_turns (tenant_id, user_id, session_id, id);

-- approval_requests: human-in-the-loop queue for high/critical-risk tool calls (Phase 10). Created only when OPA's
-- tool-stage decision is reason='requires_approval'; args are stored verbatim at creation time and never regenerated
-- -- a decision approves exactly this request or nothing (approval integrity: the plan's own "never execute a
-- different action than the one approved" rule).
CREATE TABLE IF NOT EXISTS approval_requests (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  session_id TEXT,
  tool TEXT NOT NULL,
  args JSONB NOT NULL,
  risk TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'rejected', 'executed', 'failed')),
  decided_by TEXT,
  decided_at TIMESTAMPTZ,
  result JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS approval_requests_tenant_status ON approval_requests (tenant_id, status);
