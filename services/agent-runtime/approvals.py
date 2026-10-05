"""Human approval workflow (Phase 10): a persisted queue for the one path OPA's tool stage never grants on its own
-- a high/critical-risk tool call. Lives in the `kb` database next to agent_memory (same credentials, no new secret).

Lifecycle: pending -> rejected | executed | failed. A row is created once, at the moment OPA denies a tool call with
reason='requires_approval', with the tool name and args exactly as the model requested them -- nothing here ever
regenerates or edits that payload. `app.py`'s decide endpoint re-checks OPA's separate `approval_execute` stage
before running it, then writes the terminal status.

`psycopg`/`config` are imported inside each DB function (not at module level), matching memory.py, so this module
can be imported on a host with no `psycopg` installed -- every `scripts/test_*.py` only needs the stdlib.
"""
import json
import uuid

VALID_DECISIONS = {"approve", "reject"}


def create(tenant_id: str, requested_by: str, session_id: str | None, tool: str, args: dict, risk: str) -> str:
    import psycopg

    import config
    # Prefixed, not a bare hex blob: a raw uuid4().hex pattern-matches "looks like a leaked API key/token" for the
    # output guardrail's self_check_output rail (console finding, 2026-10-04) even though it is only a reference
    # number the caller is meant to read back to a human approver.
    approval_id = f"APR-{uuid.uuid4().hex[:12]}"
    with psycopg.connect(config.KB_DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO approval_requests (id, tenant_id, requested_by, session_id, tool, args, risk) "
            "VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)",
            (approval_id, tenant_id, requested_by, session_id, tool, json.dumps(args), risk))
    return approval_id


def _row_to_dict(cols: list[str], row: tuple) -> dict:
    return dict(zip(cols, row))


_COLUMNS = ["id", "tenant_id", "requested_by", "session_id", "tool", "args", "risk", "status",
            "decided_by", "decided_at", "result", "created_at"]


def get(approval_id: str) -> dict | None:
    import psycopg

    import config
    with psycopg.connect(config.KB_DATABASE_URL) as conn:
        row = conn.execute(
            f"SELECT {','.join(_COLUMNS)} FROM approval_requests WHERE id=%s", (approval_id,)).fetchone()
    return _row_to_dict(_COLUMNS, row) if row else None


def list_pending(tenant_id: str) -> list[dict]:
    import psycopg

    import config
    with psycopg.connect(config.KB_DATABASE_URL) as conn:
        rows = conn.execute(
            f"SELECT {','.join(_COLUMNS)} FROM approval_requests WHERE tenant_id=%s AND status='pending' ORDER BY created_at",
            (tenant_id,)).fetchall()
    return [_row_to_dict(_COLUMNS, r) for r in rows]


def mark(approval_id: str, status: str, decided_by: str, result: dict | None = None) -> None:
    """status: 'rejected' | 'executed' | 'failed'. Only ever called once per row (decide checks status=='pending' first)."""
    import psycopg

    import config
    with psycopg.connect(config.KB_DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "UPDATE approval_requests SET status=%s, decided_by=%s, decided_at=now(), result=%s::jsonb WHERE id=%s",
            (status, decided_by, json.dumps(result) if result is not None else None, approval_id))
