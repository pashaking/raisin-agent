"""Controlled agent memory (Phase 8): conversation task state and user preferences, separated by purpose and
written only through a validated, policy-gated tool call -- never free-form by the model. Lives in the `kb`
database: agent-runtime already holds credentials there (it is not the system of record), so this needs no new
secret or database. Validation (`validate_key`, `validate_value`) is pure, stdlib-only, and unit-tested without a
stack; `psycopg`/`config` are imported inside the two DB functions (not at module level) so that unit test can
import this module on a host that has no `psycopg` installed, matching every other `scripts/test_*.py`.
"""
import json
import re

_KEY_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")
MAX_VALUE_BYTES = 4000


def validate_key(key: str) -> str | None:
    return None if _KEY_RE.match(key or "") else "key must match ^[a-zA-Z0-9_.-]{1,64}$"


def validate_value(value) -> str | None:
    try:
        encoded = json.dumps(value)
    except (TypeError, ValueError):
        return "value must be JSON-serializable"
    if len(encoded.encode()) > MAX_VALUE_BYTES:
        return f"value must be at most {MAX_VALUE_BYTES} bytes JSON-encoded"
    return None


def _rows(kind: str, tenant_id: str, user_id: str, session_id: str) -> list[dict]:
    import psycopg
    from opentelemetry import trace

    import config
    tracer = trace.get_tracer("agent-runtime.memory")
    with tracer.start_as_current_span(f"db.query agent_memory.{kind}") as span:
        span.set_attribute("db.system", "postgresql")
        span.set_attribute("db.operation", "SELECT")
        span.set_attribute("db.sql.table", "agent_memory")
        with psycopg.connect(config.KB_DATABASE_URL) as conn:
            rows = conn.execute(
                "SELECT key, value FROM agent_memory WHERE tenant_id=%s AND user_id=%s AND kind=%s AND session_id=%s ORDER BY key",
                (tenant_id, user_id, kind, session_id)).fetchall()
        span.set_attribute("db.rows", len(rows))
    return [{"key": k, "value": v} for k, v in rows]


def get_user_context(tenant_id: str, user_id: str) -> list[dict]:
    """Long-lived preferences for this caller. Read-only to the agent: nothing in the tool layer can write kind='preference'."""
    return _rows("preference", tenant_id, user_id, "")


def get_session_context(tenant_id: str, user_id: str, session_id: str) -> list[dict]:
    """Task state saved earlier in this same session via save_task_state."""
    return _rows("task_state", tenant_id, user_id, session_id)


def save_task_state(tenant_id: str, user_id: str, session_id: str, key: str, value) -> None:
    """Caller validates key/value with validate_key/validate_value first; this just persists."""
    import psycopg
    from opentelemetry import trace

    import config
    tracer = trace.get_tracer("agent-runtime.memory")
    with tracer.start_as_current_span("db.upsert agent_memory.task_state") as span:
        span.set_attribute("db.system", "postgresql")
        span.set_attribute("db.operation", "UPSERT")
        span.set_attribute("db.sql.table", "agent_memory")
        with psycopg.connect(config.KB_DATABASE_URL, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO agent_memory (tenant_id, user_id, kind, session_id, key, value) "
                "VALUES (%s,%s,'task_state',%s,%s,%s::jsonb) "
                "ON CONFLICT (tenant_id, user_id, kind, session_id, key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()",
                (tenant_id, user_id, session_id, key, json.dumps(value)))


def get_conversation_history(tenant_id: str, user_id: str, session_id: str) -> list[dict]:
    """Prior turns in this session, oldest first -- spliced between the system prompt and the current question
    on the next /run. Scoped by (tenant_id, user_id, session_id) from verified claims + the caller's session_id,
    same isolation as task_state; never addressed by a model-supplied argument."""
    import psycopg
    from opentelemetry import trace

    import config
    tracer = trace.get_tracer("agent-runtime.memory")
    with tracer.start_as_current_span("db.query agent_memory.conversation") as span:
        span.set_attribute("db.system", "postgresql")
        span.set_attribute("db.operation", "SELECT")
        span.set_attribute("db.sql.table", "conversation_turns")
        with psycopg.connect(config.KB_DATABASE_URL) as conn:
            rows = conn.execute(
                "SELECT role, content FROM conversation_turns WHERE tenant_id=%s AND user_id=%s AND session_id=%s "
                "ORDER BY id DESC LIMIT %s",
                (tenant_id, user_id, session_id, config.MAX_HISTORY_TURNS)).fetchall()
        span.set_attribute("db.rows", len(rows))
    return [{"role": r, "content": c} for r, c in reversed(rows)]


def save_conversation_turn(tenant_id: str, user_id: str, session_id: str, role: str, content: str) -> None:
    """Appends one turn (the sanitized user question or the final post-guardrail answer) for replay by
    get_conversation_history on the next /run in this session."""
    import psycopg
    from opentelemetry import trace

    import config
    tracer = trace.get_tracer("agent-runtime.memory")
    with tracer.start_as_current_span("db.insert agent_memory.conversation") as span:
        span.set_attribute("db.system", "postgresql")
        span.set_attribute("db.operation", "INSERT")
        span.set_attribute("db.sql.table", "conversation_turns")
        with psycopg.connect(config.KB_DATABASE_URL, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO conversation_turns (tenant_id, user_id, session_id, role, content) VALUES (%s,%s,%s,%s,%s)",
                (tenant_id, user_id, session_id, role, content))
