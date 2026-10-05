"""Tool layer: narrow, policy-scoped functions. The runtime holds no database credentials for the
system of record; donation, transaction and donor facts come from raisin-api, already minimized.

Every tool: OPA tool-stage decision (ABAC on the resource's owning tenant) -> raisin-api with the caller's JWT
(re-checks tenant) -> minimized result. Tenant is never a tool argument. For by-id tools, a missing id and a
foreign-tenant id look identical to the model (`not_found`); the OPA deny is still recorded in the trace/SIEM.
Placeholder tokens the input guardrail produced (<EMAIL_ADDRESS_1>) are resolved here, so the model never
sees the donor's real email."""
import json

import httpx
import psycopg
from opentelemetry import trace

import approvals
import config
import guardrails
import llm
import memory
import policy

tracer = trace.get_tracer("agent-runtime.tools")

_NO_ARGS = {"type": "object", "properties": {}}


def _fn(name: str, description: str, params: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": params}}


SCHEMAS = {
    "get_transaction_analysis": _fn(
        "get_transaction_analysis",
        "Get the minimized analysis of a donation transaction (amount, result, decline category, fraud score) by transaction id.",
        {"type": "object", "properties": {"transaction_id": {"type": "integer"}}, "required": ["transaction_id"]}),
    "get_donation": _fn(
        "get_donation",
        "Get one donation of the caller's charity by donation id: amount, currency, status, donor id and its transactions "
        "(result, decline category). Returns not_found if the id is unknown to this charity.",
        {"type": "object", "properties": {"donation_id": {"type": "integer"}}, "required": ["donation_id"]}),
    "list_transactions": _fn(
        "list_transactions",
        "List the caller's charity's transactions, newest first, optionally filtered by result (approved|declined), "
        "decline_category (insufficient_funds|do_not_honor|invalid_card|expired_card), min_amount, max_amount. limit <= 50.",
        {"type": "object", "properties": {
            "result": {"type": "string", "enum": ["approved", "declined"]},
            "decline_category": {"type": "string", "enum": ["insufficient_funds", "do_not_honor", "invalid_card", "expired_card"]},
            "min_amount": {"type": "number"}, "max_amount": {"type": "number"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50}}}),
    "get_tenant_donation_summary": _fn(
        "get_tenant_donation_summary",
        "Aggregates for the caller's charity: donation count, approved/declined counts and totals, declines by category.",
        _NO_ARGS),
    "get_donor_profile": _fn(
        "get_donor_profile",
        "Donor giving summary by donor id: donation count, total approved, last status. Never returns name, email or phone.",
        {"type": "object", "properties": {"donor_id": {"type": "integer"}}, "required": ["donor_id"]}),
    "find_donor": _fn(
        "find_donor",
        "Find a donor of the caller's charity by email address and return the giving summary (donor id, donation count, "
        "total approved, last status). Pass the email exactly as written in the user's message, including placeholder "
        "tokens such as <EMAIL_ADDRESS_1>.",
        {"type": "object", "properties": {"email": {"type": "string"}}, "required": ["email"]}),
    "get_my_donations": _fn(
        "get_my_donations",
        "The signed-in donor's own donations (id, amount, currency, status). No arguments.",
        _NO_ARGS),
    "search_kb": _fn(
        "search_kb",
        "Search policies, procedures, runbooks and FAQs the caller is entitled to read.",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}),
    # The one write action in the registry (Phase 10): action_risk=high, so OPA's tool stage always denies it with
    # requires_approval -- offered to Finance, but every call creates a pending approval request instead of running.
    "resend_receipt": _fn(
        "resend_receipt",
        "Resend the tax receipt for a donation. This is a real action, not a read, and requires human approval: the "
        "call returns a pending approval_id, not a result. Tell the user their request needs sign-off.",
        {"type": "object", "properties": {"donation_id": {"type": "integer"}}, "required": ["donation_id"]}),
    # Controlled agent memory (Phase 8): separated by purpose, policy-gated like every other tool.
    "get_user_context": _fn(
        "get_user_context",
        "Get the caller's saved long-term preferences, if any. Read-only: this agent cannot set preferences.",
        _NO_ARGS),
    "get_session_context": _fn(
        "get_session_context",
        "Get task state saved earlier in this same session via save_task_state. Empty if no session is active.",
        _NO_ARGS),
    "save_task_state": _fn(
        "save_task_state",
        "Save one small, named piece of state (a short key and a JSON-serializable value) for later steps in this "
        "same session, e.g. an intermediate finding to reuse. Not for donor, donation or transaction facts -- those "
        "come from the data tools, not memory.",
        {"type": "object", "properties": {"key": {"type": "string"}, "value": {}}, "required": ["key", "value"]}),
    # Transaction-investigation tool set (plan Phase 3, literal names): campaign/gateway/fraud/error/incident
    # investigation tools, additive to the donor/donation tools above. All action_risk=low (Phase 1: READ+ANALYZE+
    # RECOMMEND only -- no MODIFY/BLOCK/REFUND tool exists in this set).
    "get_campaign": _fn(
        "get_campaign",
        "Get one campaign of the caller's charity by campaign id: name, status, goal amount. Returns not_found if "
        "the id is unknown to this charity.",
        {"type": "object", "properties": {"campaign_id": {"type": "integer"}}, "required": ["campaign_id"]}),
    "get_campaign_statistics": _fn(
        "get_campaign_statistics",
        "Aggregates for one campaign of the caller's charity: transaction_count, successful_count, declined_count, "
        "success_rate, total_amount, declines by category. Returns not_found if the campaign id is unknown to this "
        "charity.",
        {"type": "object", "properties": {"campaign_id": {"type": "integer"}}, "required": ["campaign_id"]}),
    "get_transaction": _fn(
        "get_transaction",
        "Get one transaction of the caller's charity by transaction id: amount, result, decline category, fraud "
        "score, gateway, campaign id, occurred_at. Returns not_found if the id is unknown to this charity.",
        {"type": "object", "properties": {"transaction_id": {"type": "integer"}}, "required": ["transaction_id"]}),
    "search_transactions": _fn(
        "search_transactions",
        "Search the caller's charity's transactions with the full filter set: result (approved|declined), "
        "decline_category, campaign_id, gateway (stripe|adyen), min_amount, max_amount, start_time/end_time "
        "(ISO 8601). limit <= 50.",
        {"type": "object", "properties": {
            "result": {"type": "string", "enum": ["approved", "declined"]},
            "decline_category": {"type": "string", "enum": ["insufficient_funds", "do_not_honor", "invalid_card", "expired_card", "processor_error"]},
            "campaign_id": {"type": "integer"}, "gateway": {"type": "string", "enum": ["stripe", "adyen"]},
            "min_amount": {"type": "number"}, "max_amount": {"type": "number"},
            "start_time": {"type": "string"}, "end_time": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50}}}),
    "get_transaction_statistics": _fn(
        "get_transaction_statistics",
        "Transaction statistics for the caller's charity, optionally scoped to one campaign and/or a time window: "
        "transaction_count, successful_count, declined_count, success_rate, total_amount. status filters to "
        "approved or declined only.",
        {"type": "object", "properties": {
            "campaign_id": {"type": "integer"}, "start_time": {"type": "string"}, "end_time": {"type": "string"},
            "status": {"type": "string", "enum": ["approved", "declined"]}}}),
    "compare_transaction_periods": _fn(
        "compare_transaction_periods",
        "Compare transaction statistics between two relative time periods for the caller's charity, optionally "
        "scoped to one campaign. current/previous are one of last_hour, previous_hour, last_24h, previous_24h "
        "(default: last_hour vs previous_hour).",
        {"type": "object", "properties": {
            "campaign_id": {"type": "integer"},
            "current": {"type": "string", "enum": ["last_hour", "previous_hour", "last_24h", "previous_24h"]},
            "previous": {"type": "string", "enum": ["last_hour", "previous_hour", "last_24h", "previous_24h"]}}}),
    "get_decline_statistics": _fn(
        "get_decline_statistics",
        "Decline breakdown by category (count, total amount) for the caller's charity, optionally scoped to one "
        "campaign and/or a time window.",
        {"type": "object", "properties": {
            "campaign_id": {"type": "integer"}, "start_time": {"type": "string"}, "end_time": {"type": "string"}}}),
    "get_payment_gateway_statistics": _fn(
        "get_payment_gateway_statistics",
        "Per-gateway transaction statistics for the caller's charity (transaction count, declined count, decline "
        "rate, total amount), optionally scoped to one campaign and/or a time window.",
        {"type": "object", "properties": {
            "campaign_id": {"type": "integer"}, "start_time": {"type": "string"}, "end_time": {"type": "string"}}}),
    "get_fraud_signals": _fn(
        "get_fraud_signals",
        "Transactions with a fraud score at or above min_fraud_score (default 0.5) for the caller's charity, "
        "optionally scoped to one campaign and/or a time window, with the flagged count and average score.",
        {"type": "object", "properties": {
            "campaign_id": {"type": "integer"}, "start_time": {"type": "string"}, "end_time": {"type": "string"},
            "min_fraud_score": {"type": "number"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}}),
    "get_application_errors": _fn(
        "get_application_errors",
        "Recent application errors logged for the caller's charity, optionally filtered by service and/or a time "
        "window.",
        {"type": "object", "properties": {
            "service": {"type": "string"}, "start_time": {"type": "string"}, "end_time": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50}}}),
    "get_incident_history": _fn(
        "get_incident_history",
        "Incident history for the caller's charity, optionally filtered by status (open|resolved) and/or a time "
        "window.",
        {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["open", "resolved"]}, "start_time": {"type": "string"},
            "end_time": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}}),
    # Controlled actions (plan Phase 14): graduated autonomy (Phase 9) -- restart_worker/clear_failed_job/
    # block_ip_temporarily are action_risk=medium, the other three are low; OPA's tool stage auto-executes both
    # tiers (unlike resend_receipt/high, which always requires_approval). No outbound system backs any of them in
    # this POC -- each is a no-op status-flip, audited server-side, same framing resend_receipt uses.
    "restart_worker": _fn(
        "restart_worker",
        "Restart a stuck or crash-looping worker process for the caller's charity, by worker name (e.g. "
        "'ingest-worker-3'). Use after application errors point to one worker repeatedly failing.",
        {"type": "object", "properties": {"worker": {"type": "string"}}, "required": ["worker"]}),
    "clear_failed_job": _fn(
        "clear_failed_job",
        "Clear one failed background job for the caller's charity by job id, so it stops blocking the queue. Use "
        "after confirming the job's failure is understood and safe to drop.",
        {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}),
    "block_ip_temporarily": _fn(
        "block_ip_temporarily",
        "Temporarily block an IP address from the caller's charity's traffic for a bounded duration (default 120 "
        "minutes, max 1440). Use for a confirmed card-testing or abuse pattern, e.g. after get_fraud_signals flags "
        "a cluster of high-fraud-score transactions from one source.",
        {"type": "object", "properties": {
            "ip": {"type": "string"}, "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 1440}},
            "required": ["ip"]}),
    "create_incident": _fn(
        "create_incident",
        "Open a new incident for the caller's charity with a title, severity (low|medium|high|critical) and a "
        "summary of what was found. Use to record an investigation's findings so they show up in "
        "get_incident_history.",
        {"type": "object", "properties": {
            "title": {"type": "string"}, "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            "summary": {"type": "string"}}, "required": ["title", "severity", "summary"]}),
    "send_notification": _fn(
        "send_notification",
        "Send a notification to the caller's charity's on-call/ops channel (slack|email|pagerduty) with a short "
        "message. Use to escalate a finding to a human rather than taking a riskier action yourself.",
        {"type": "object", "properties": {
            "channel": {"type": "string", "enum": ["slack", "email", "pagerduty"]}, "message": {"type": "string"}},
            "required": ["channel", "message"]}),
    "collect_diagnostic_bundle": _fn(
        "collect_diagnostic_bundle",
        "Collect a diagnostic bundle for the caller's charity: recent application error count and open incident "
        "count, optionally scoped to one service and/or a time window. Use before escalating, to attach context.",
        {"type": "object", "properties": {
            "service": {"type": "string"}, "start_time": {"type": "string"}, "end_time": {"type": "string"}}}),
}

NOT_FOUND = {"error": "not_found", "message": "No such record for your charity."}
DENIED = {"error": "denied", "message": "You are not authorized to access this record."}


def schemas_for(allowed: list[str]) -> list[dict]:
    return [SCHEMAS[n] for n in allowed if n in SCHEMAS]


# ---------- helpers ----------
def _pip_tenant(kind: str, row_id: int) -> str | None:
    """Policy Information Point: owning tenant of a transaction / donation / donor, or None."""
    with tracer.start_as_current_span(f"pip.{kind}_tenant") as span:
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute(f"{kind}.id", row_id)
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{config.RAISIN_API_URL}/internal/{kind}s/{row_id}/tenant", headers={"X-Service-Token": config.RUNTIME_SERVICE_TOKEN})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()["tenant_id"]


def _decide(name: str, ctx: dict, resource_tenant: str, span) -> bool:
    d = policy.decide("tool", ctx["claims"], tool=name, resource={"tenant_id": resource_tenant})
    ctx["decisions"].append({"stage": "tool", "tool": name, "allow": d.get("allow"), "reason": d.get("reason"), "risk": d.get("risk")})
    if not d.get("allow"):
        span.set_attribute("tool.result", "denied")
        span.set_attribute("tool.deny_reason", d.get("reason", ""))
        return False
    return True


def _api(ctx: dict, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> tuple[int, dict]:
    with httpx.Client(timeout=10) as c:
        r = c.request(method, f"{config.RAISIN_API_URL}{path}", params=params, json=body,
                      headers={"Authorization": ctx["authorization"], "X-Service-Token": config.RUNTIME_SERVICE_TOKEN})
    try:
        payload = r.json()
    except ValueError:
        payload = {}
    return r.status_code, payload


def _resolve(value: str, ctx: dict) -> str:
    """Map an input-guardrail placeholder (<EMAIL_ADDRESS_1>) back to the real value, in-process only."""
    ph = ctx.get("placeholders")
    v = str(value or "").strip()
    if ph is not None and v in ph.mapping:
        return ph.mapping[v]
    return v


def _ok(span, out: dict) -> dict:
    span.set_attribute("tool.result", "ok")
    span.set_attribute("tool.result.fields", sorted(out.keys()))
    return out


def _int(args: dict, key: str) -> int:
    try:
        return int(args.get(key, 0))
    except (TypeError, ValueError):
        return 0


def _params(args: dict, keys: tuple[str, ...]) -> dict:
    return {k: args[k] for k in keys if args.get(k) not in (None, "")}


# ---------- tool handlers ----------
def _by_id(name: str, kind: str, id_key: str, path: str, args: dict, ctx: dict, span) -> dict:
    """Shared path for get_transaction_analysis / get_donation / get_donor_profile: PIP -> OPA (ABAC) -> raisin-api.
    Missing, foreign and backend-denied ids all come back as not_found; the deny itself is still in the trace (and in the
    decisions list, which reaches the end user only with DEBUG_PANEL=on)."""
    row_id = _int(args, id_key)
    owner = _pip_tenant(kind, row_id)
    if owner is None:
        span.set_attribute("tool.result", "not_found")
        return NOT_FOUND
    if not _decide(name, ctx, owner, span):
        return NOT_FOUND
    st, out = _api(ctx, "GET", path.format(id=row_id))
    if st in (403, 404):
        span.set_attribute("tool.result", "not_found")
        return NOT_FOUND
    if st != 200:
        raise RuntimeError(f"raisin-api {st}")
    return _ok(span, out)


def _tenant_scoped(name: str, method: str, path: str, ctx: dict, span, *, params: dict | None = None, body: dict | None = None) -> dict:
    """Shared path for list/summary/self tools: resource tenant is the caller's own; raisin-api filters in SQL."""
    if not _decide(name, ctx, ctx["claims"]["tenant_id"], span):
        return {**DENIED, "reason": ctx["decisions"][-1]["reason"]}
    st, out = _api(ctx, method, path, params=params, body=body)
    if st == 404:
        span.set_attribute("tool.result", "not_found")
        return NOT_FOUND
    if st == 422:
        span.set_attribute("tool.result", "bad_args")
        return {"error": "bad_args", "message": str(out.get("detail", "invalid arguments"))[:200]}
    if st != 200:
        raise RuntimeError(f"raisin-api {st}")
    return _ok(span, out)


def _list_transactions(args: dict, ctx: dict, span) -> dict:
    params = {k: args[k] for k in ("result", "decline_category", "min_amount", "max_amount", "limit") if args.get(k) not in (None, "")}
    return _tenant_scoped("list_transactions", "GET", "/api/transactions", ctx, span, params=params)


def _find_donor(args: dict, ctx: dict, span) -> dict:
    email = _resolve(args.get("email", ""), ctx)  # real value stays in-process: JSON body, never a span attribute or URL
    span.set_attribute("tool.arg.resolved_placeholder", str(args.get("email", "")) != email)
    if not email:
        return {"error": "bad_args", "message": "email is required"}
    return _tenant_scoped("find_donor", "POST", "/api/donors/lookup", ctx, span, body={"email": email})


def _search_kb(args: dict, ctx: dict, span) -> dict:
    claims = ctx["claims"]
    if not _decide("search_kb", ctx, claims["tenant_id"], span):
        return {"error": "denied", "reason": ctx["decisions"][-1]["reason"]}
    flt = ctx["retrieval_filter"]
    query = str(args.get("query", ""))
    with tracer.start_as_current_span("retrieve search_kb") as rspan:
        rspan.set_attribute("openinference.span.kind", "RETRIEVER")
        rspan.set_attribute("retrieval.filter.tenants", flt["tenants"])
        rspan.set_attribute("retrieval.filter.classifications", flt["classifications"])
        vec = llm.embed(ctx["client"], query)
        with tracer.start_as_current_span("db.query") as qspan:
            qspan.set_attribute("db.system", "postgresql")
            qspan.set_attribute("db.operation", "SELECT")
            qspan.set_attribute("db.sql.table", "chunks")
            with psycopg.connect(config.KB_DATABASE_URL) as conn:
                rows = conn.execute(
                    "SELECT doc_id, chunk_id, tenant_id, classification, content, 1 - (embedding <=> %s::vector) AS score "
                    "FROM chunks WHERE tenant_id = ANY(%s) AND classification = ANY(%s) "
                    "ORDER BY embedding <=> %s::vector LIMIT 4",
                    (json.dumps(vec), flt["tenants"], flt["classifications"], json.dumps(vec))).fetchall()
            qspan.set_attribute("db.rows", len(rows))
        chunks = [{"doc_id": r[0], "chunk_id": r[1], "tenant_id": r[2], "classification": r[3], "content": r[4], "score": float(r[5])} for r in rows]
        rspan.set_attribute("retrieval.documents", [f"{c['doc_id']}#{c['chunk_id']}:{c['score']:.3f}" for c in chunks])
    flagged = guardrails.retrieved_stage(chunks)
    ctx["guardrail_info"]["retrieved"] = {"chunks": len(chunks), "flagged": flagged, "docs": [c["doc_id"] for c in chunks],
                                          "flag_reasons": {c["doc_id"]: c.get("flag_reasons", []) for c in chunks if c.get("flagged")}}
    span.set_attribute("tool.result", "ok")
    return {"results": [{"doc_id": c["doc_id"], "classification": c["classification"], "content": c["content"]} for c in chunks]}


def _get_user_context(args: dict, ctx: dict, span) -> dict:
    claims = ctx["claims"]
    if not _decide("get_user_context", ctx, claims["tenant_id"], span):
        return {**DENIED, "reason": ctx["decisions"][-1]["reason"]}
    return _ok(span, {"preferences": memory.get_user_context(claims["tenant_id"], claims["sub"])})


def _get_session_context(args: dict, ctx: dict, span) -> dict:
    claims = ctx["claims"]
    if not _decide("get_session_context", ctx, claims["tenant_id"], span):
        return {**DENIED, "reason": ctx["decisions"][-1]["reason"]}
    session_id = ctx.get("session_id")
    if not session_id:
        return {"error": "bad_args", "message": "no session_id on this run; nothing to recall."}
    return _ok(span, {"task_state": memory.get_session_context(claims["tenant_id"], claims["sub"], session_id)})


def _save_task_state(args: dict, ctx: dict, span) -> dict:
    claims = ctx["claims"]
    if not _decide("save_task_state", ctx, claims["tenant_id"], span):
        return {**DENIED, "reason": ctx["decisions"][-1]["reason"]}
    session_id = ctx.get("session_id")
    if not session_id:
        return {"error": "bad_args", "message": "no session_id on this run; nothing to save against."}
    key = str(args.get("key", ""))
    value = args.get("value")
    err = memory.validate_key(key) or memory.validate_value(value)
    if err:
        return {"error": "bad_args", "message": err}
    memory.save_task_state(claims["tenant_id"], claims["sub"], session_id, key, value)
    return _ok(span, {"saved": True, "key": key})


def _call_resend_receipt(ctx: dict, donation_id: int, span) -> dict:
    st, out = _api(ctx, "POST", f"/api/donations/{donation_id}/resend-receipt")
    if st in (403, 404):
        span.set_attribute("tool.result", "not_found")
        return NOT_FOUND
    if st != 200:
        raise RuntimeError(f"raisin-api {st}")
    return _ok(span, out)


def _resend_receipt(args: dict, ctx: dict, span) -> dict:
    """PIP -> OPA(resource tenant), same shape as _by_id, but a requires_approval deny creates a persisted approval
    request (Phase 10) instead of a dead end. args are stored exactly as the model sent them; nothing here, or in
    the decide endpoint, ever regenerates them."""
    donation_id = _int(args, "donation_id")
    owner = _pip_tenant("donation", donation_id)
    if owner is None:
        span.set_attribute("tool.result", "not_found")
        return NOT_FOUND
    d = policy.decide("tool", ctx["claims"], tool="resend_receipt", resource={"tenant_id": owner})
    ctx["decisions"].append({"stage": "tool", "tool": "resend_receipt", "allow": d.get("allow"), "reason": d.get("reason"), "risk": d.get("risk")})
    if d.get("allow"):
        # action_risk is "high" on every seeded tool row, so OPA's tool stage never actually allows resend_receipt
        # directly today; kept so a future registry downgrade to low/medium works without touching this handler.
        return _call_resend_receipt(ctx, donation_id, span)
    reason = d.get("reason")
    if reason == "tenant_mismatch":
        span.set_attribute("tool.result", "not_found")
        return NOT_FOUND
    if reason == "requires_approval":
        claims = ctx["claims"]
        approval_id = approvals.create(owner, claims["sub"], ctx.get("session_id"), "resend_receipt",
                                        {"donation_id": donation_id}, d.get("risk", "high"))
        span.set_attribute("tool.result", "requires_approval")
        span.set_attribute("approval.id", approval_id)
        return {"error": "requires_approval", "approval_id": approval_id,
                "message": f"This action needs a human approver for your charity to review and sign off on request "
                           f"{approval_id} before it runs."}
    span.set_attribute("tool.result", "denied")
    return {**DENIED, "reason": reason}


def execute_approved(tool: str, args: dict, ctx: dict) -> dict:
    """Called only by the /approvals decide endpoint, after a fresh OPA approval_execute allow -- runs the exact
    stored tool/args, with no model or tool loop involved. ctx needs only 'authorization' (the approver's own JWT;
    raisin-api re-checks role+tenant via its own authorize(), and approval_execute already confirmed the resource
    tenant equals the approver's own tenant)."""
    with tracer.start_as_current_span(f"execute_approved {tool}") as span:
        span.set_attribute("gen_ai.tool.name", tool)
        span.set_attribute("openinference.span.kind", "TOOL")
        span.set_attribute("tool.name", tool)
        if tool == "resend_receipt":
            return _call_resend_receipt(ctx, _int(args, "donation_id"), span)
        span.set_attribute("tool.result", "unknown_tool")
        return {"error": "unknown_tool", "tool": tool}


HANDLERS = {
    # uniform not_found on missing, foreign and backend-denied ids (CSO F6): the OPA deny is in the trace, not in the model's view
    "get_transaction_analysis": lambda a, c, s: _by_id("get_transaction_analysis", "transaction", "transaction_id", "/api/transactions/{id}/analysis", a, c, s),
    "get_donation": lambda a, c, s: _by_id("get_donation", "donation", "donation_id", "/api/donations/{id}", a, c, s),
    "get_donor_profile": lambda a, c, s: _by_id("get_donor_profile", "donor", "donor_id", "/api/donors/{id}/profile", a, c, s),
    "list_transactions": _list_transactions,
    "get_tenant_donation_summary": lambda a, c, s: _tenant_scoped("get_tenant_donation_summary", "GET", "/api/summary", c, s),
    "find_donor": _find_donor,
    "get_my_donations": lambda a, c, s: _tenant_scoped("get_my_donations", "GET", "/api/me/donations", c, s),
    "search_kb": _search_kb,
    "get_user_context": _get_user_context,
    "get_session_context": _get_session_context,
    "save_task_state": _save_task_state,
    "resend_receipt": _resend_receipt,
    # transaction-investigation tool set (Phase 3)
    "get_campaign": lambda a, c, s: _by_id("get_campaign", "campaign", "campaign_id", "/api/campaigns/{id}", a, c, s),
    "get_campaign_statistics": lambda a, c, s: _by_id("get_campaign_statistics", "campaign", "campaign_id", "/api/campaigns/{id}/statistics", a, c, s),
    "get_transaction": lambda a, c, s: _by_id("get_transaction", "transaction", "transaction_id", "/api/transactions/{id}", a, c, s),
    "search_transactions": lambda a, c, s: _tenant_scoped(
        "search_transactions", "GET", "/api/transaction-search", c, s,
        params=_params(a, ("result", "decline_category", "campaign_id", "gateway", "min_amount", "max_amount", "start_time", "end_time", "limit"))),
    "get_transaction_statistics": lambda a, c, s: _tenant_scoped(
        "get_transaction_statistics", "GET", "/api/transaction-statistics", c, s,
        params=_params(a, ("campaign_id", "start_time", "end_time", "status"))),
    "compare_transaction_periods": lambda a, c, s: _tenant_scoped(
        "compare_transaction_periods", "GET", "/api/transaction-comparison", c, s,
        params=_params(a, ("campaign_id", "current", "previous"))),
    "get_decline_statistics": lambda a, c, s: _tenant_scoped(
        "get_decline_statistics", "GET", "/api/decline-statistics", c, s,
        params=_params(a, ("campaign_id", "start_time", "end_time"))),
    "get_payment_gateway_statistics": lambda a, c, s: _tenant_scoped(
        "get_payment_gateway_statistics", "GET", "/api/gateway-statistics", c, s,
        params=_params(a, ("campaign_id", "start_time", "end_time"))),
    "get_fraud_signals": lambda a, c, s: _tenant_scoped(
        "get_fraud_signals", "GET", "/api/fraud-signals", c, s,
        params=_params(a, ("campaign_id", "start_time", "end_time", "min_fraud_score", "limit"))),
    "get_application_errors": lambda a, c, s: _tenant_scoped(
        "get_application_errors", "GET", "/api/application-errors", c, s,
        params=_params(a, ("service", "start_time", "end_time", "limit"))),
    "get_incident_history": lambda a, c, s: _tenant_scoped(
        "get_incident_history", "GET", "/api/incidents", c, s,
        params=_params(a, ("status", "start_time", "end_time", "limit"))),
    # controlled actions (Phase 14): medium/low action_risk, so OPA's tool stage auto-executes these like any other
    # tenant-scoped tool -- no approval branching needed here, unlike resend_receipt.
    "restart_worker": lambda a, c, s: _tenant_scoped(
        "restart_worker", "POST", "/api/workers/restart", c, s, body=_params(a, ("worker",))),
    "clear_failed_job": lambda a, c, s: _tenant_scoped(
        "clear_failed_job", "POST", "/api/jobs/clear", c, s, body=_params(a, ("job_id",))),
    "block_ip_temporarily": lambda a, c, s: _tenant_scoped(
        "block_ip_temporarily", "POST", "/api/security/block-ip", c, s, body=_params(a, ("ip", "duration_minutes"))),
    "create_incident": lambda a, c, s: _tenant_scoped(
        "create_incident", "POST", "/api/incidents", c, s, body=_params(a, ("title", "severity", "summary"))),
    "send_notification": lambda a, c, s: _tenant_scoped(
        "send_notification", "POST", "/api/notifications", c, s, body=_params(a, ("channel", "message"))),
    "collect_diagnostic_bundle": lambda a, c, s: _tenant_scoped(
        "collect_diagnostic_bundle", "POST", "/api/diagnostics/collect", c, s,
        body=_params(a, ("service", "start_time", "end_time"))),
}


def execute(name: str, args: dict, ctx: dict) -> dict:
    """ctx: claims, authorization, client (OpenAI for caller key), retrieval_filter, guardrail_info, placeholders, decisions, session_id"""
    with tracer.start_as_current_span(f"execute_tool {name}") as span:
        span.set_attribute("gen_ai.tool.name", name)
        span.set_attribute("openinference.span.kind", "TOOL")
        span.set_attribute("tool.name", name)
        # model-supplied args (placeholders, ids). Full values only with DEBUG_TRACE_CONTENT=on; otherwise keys + sha256 (CSO F1).
        span.set_attribute("tool.parameters", llm.content_attr(args, keys=sorted(args.keys())))
        handler = HANDLERS.get(name)
        if handler is None:
            span.set_attribute("tool.result", "unknown_tool")
            return {"error": "unknown_tool", "tool": name}
        return handler(args, ctx, span)
