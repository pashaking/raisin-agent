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

import config
import guardrails
import llm
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
    # Registered and approved, but in no role's allow-list, so it is never offered to the model.
    "resend_receipt": _fn(
        "resend_receipt",
        "Resend the tax receipt for a donation.",
        {"type": "object", "properties": {"donation_id": {"type": "integer"}}, "required": ["donation_id"]}),
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
    ctx["decisions"].append({"stage": "tool", "tool": name, "allow": d.get("allow"), "reason": d.get("reason")})
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
}


def execute(name: str, args: dict, ctx: dict) -> dict:
    """ctx: claims, authorization, client (OpenAI for caller key), retrieval_filter, guardrail_info, placeholders, decisions"""
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
