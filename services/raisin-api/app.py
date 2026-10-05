"""raisin-api: identity route (JWT + JWKS), application API, and system-of-record API.

Diagram-01 boxes it covers: Identity (Entra ID stand-in), Application APIs, Backend services + RDS.
The payment path (/donate -> payment-gateway) never touches the AI path (/assistant, /story -> agent-runtime).
"""
import ipaddress
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field

import db
from keys import load_or_generate
from otel_setup import current_trace_id, init_tracing, instrument_fastapi

tracer = init_tracing("raisin-api")
app = FastAPI(title="raisin-api")
instrument_fastapi(app)

PRIV_PEM, PUB_PEM, JWKS, KID = load_or_generate()
RUNTIME_URL = os.environ["AGENT_RUNTIME_URL"].rstrip("/")
PAYGW_URL = os.environ["PAYMENT_GATEWAY_URL"].rstrip("/")
SERVICE_TOKEN = os.environ["RUNTIME_SERVICE_TOKEN"]
ISSUER = "raisin-api"
AUDIENCE = "aicp"

DECLINE_CATEGORY = {"51": "insufficient_funds", "05": "do_not_honor", "14": "invalid_card", "54": "expired_card",
                    "96": "processor_error"}  # gateway-side failure (Phase 3 transaction-investigation tool set), not a card decline


# ---------- identity ----------
class TokenReq(BaseModel):
    user: str


@app.post("/auth/token")
def auth_token(req: TokenReq):
    u = db.query("SELECT email, tenant_id, role FROM users WHERE email = %s", (req.user,), one=True, table="users")
    if not u:
        raise HTTPException(401, "unknown user")
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": u["email"], "tenant_id": u["tenant_id"], "role": u["role"],
              "iat": now, "exp": now + 900, "jti": str(uuid.uuid4())}
    token = jwt.encode(claims, PRIV_PEM, algorithm="RS256", headers={"kid": KID})
    span = trace.get_current_span()
    span.set_attribute("enduser.id", u["email"])
    span.set_attribute("tenant_id", u["tenant_id"])
    span.set_attribute("role", u["role"])
    return {"access_token": token, "token_type": "Bearer", "expires_in": 900, "tenant_id": u["tenant_id"], "role": u["role"]}


@app.get("/auth/users")
def auth_users():
    """Demo-only identity picker for the chat console (/ui). A real IdP owns this list; here it is the seeded users."""
    return db.query("SELECT email, tenant_id, role FROM users ORDER BY role, email", table="users")


@app.get("/.well-known/jwks.json")
def jwks():
    return JWKS


def verify(authorization: str | None) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    try:
        claims = jwt.decode(authorization.split(" ", 1)[1], PUB_PEM, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER)
    except jwt.PyJWTError as e:
        raise HTTPException(401, f"invalid token: {e}") from e
    span = trace.get_current_span()
    span.set_attribute("enduser.id", claims["sub"])
    span.set_attribute("tenant_id", claims["tenant_id"])
    span.set_attribute("role", claims["role"])
    return claims


# ---------- payment path (never AI) ----------
class Donor(BaseModel):
    email: str
    phone: str | None = None
    name: str | None = None


class DonateReq(BaseModel):
    """Public form payload. No donation_id: the server assigns one (CSO F2). extra=forbid rejects any attempt to send one."""
    model_config = ConfigDict(extra="forbid")
    tenant_id: str
    amount: float = Field(gt=0)
    currency: str = "CAD"
    payment_token: str
    donor: Donor


@app.post("/donate")
def donate(req: DonateReq):
    """Public donation form (no JWT). Integrity rules (CSO F2): the tenant must exist, the donation id comes from a
    sequence, rows are INSERT-only (no ON CONFLICT ... UPDATE on donations/transactions), and an existing donor's contact
    fields are never overwritten by a public post. Forwards only {amount, currency, payment_token} to the payment
    gateway. Donor fields never reach a span."""
    span = trace.get_current_span()
    span.set_attribute("tenant_id", req.tenant_id)
    if not db.query("SELECT id FROM tenants WHERE id = %s", (req.tenant_id,), one=True, table="tenants"):
        raise HTTPException(404, "unknown tenant")
    donor = db.query(
        "INSERT INTO donors (tenant_id, email, phone, name) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT (tenant_id, email) DO UPDATE SET phone = COALESCE(donors.phone, EXCLUDED.phone), name = COALESCE(donors.name, EXCLUDED.name) "
        "RETURNING id",
        (req.tenant_id, req.donor.email, req.donor.phone, req.donor.name), one=True, table="donors")
    donation_id = db.query("SELECT nextval('donation_id_seq') AS id", one=True, table="donation_id_seq")["id"]
    span.set_attribute("donation.id", donation_id)
    db.query("INSERT INTO donations (id, tenant_id, donor_id, amount, currency, status) VALUES (%s,%s,%s,%s,%s,'pending')",
             (donation_id, req.tenant_id, donor["id"], req.amount, req.currency), table="donations")
    with httpx.Client(timeout=10) as c:
        r = c.post(f"{PAYGW_URL}/charge", json={"amount": req.amount, "currency": req.currency, "payment_token": req.payment_token})
    r.raise_for_status()
    res = r.json()
    db.query("INSERT INTO transactions (id, donation_id, tenant_id, amount, currency, result, decline_code, fraud_score) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
             (donation_id, donation_id, req.tenant_id, req.amount, req.currency, res["result"], res.get("decline_code"), 0.10), table="transactions")
    db.query("UPDATE donations SET status = %s WHERE id = %s AND tenant_id = %s", (res["result"], donation_id, req.tenant_id), table="donations")
    span.set_attribute("payment.result", res["result"])
    return {"donation_id": donation_id, "result": res["result"], "decline_code": res.get("decline_code"), "trace_id": current_trace_id()}


# ---------- AI path (forwarded to the agent runtime with claims + traceparent) ----------
class AssistantReq(BaseModel):
    question: str
    model: str | None = None
    temperature: float | None = None
    session_id: str | None = None  # passed through to agent-runtime for get_session_context/save_task_state (Phase 8)


class StoryReq(BaseModel):
    free_text: str
    model: str | None = None


def _forward(path: str, body: dict, authorization: str, response: Response):
    with httpx.Client(timeout=120) as c:
        r = c.post(f"{RUNTIME_URL}{path}", json=body, headers={"Authorization": authorization})
    response.headers["X-Trace-Id"] = current_trace_id()
    payload = r.json()
    if isinstance(payload, dict):
        payload.setdefault("trace_id", current_trace_id())
    response.status_code = r.status_code
    return payload


@app.post("/assistant")
def assistant(req: AssistantReq, response: Response, authorization: str | None = Header(default=None)):
    verify(authorization)
    return _forward("/run", req.model_dump(exclude_none=True), authorization, response)


@app.post("/story")
def story(req: StoryReq, response: Response, authorization: str | None = Header(default=None)):
    verify(authorization)
    return _forward("/story", req.model_dump(exclude_none=True), authorization, response)


# ---------- system-of-record API (tools call these) ----------
# Defense in depth for the AI path, mirroring what OPA decides in the runtime:
#   1. X-Service-Token: only agent-runtime may call /api/* (end users never reach tool-backing routes directly);
#   2. the caller's JWT must be valid;
#   3. the JWT role must be allowed the tool that this endpoint backs, per the same policy/data/roles.json OPA uses;
#   4. every query filters by the JWT tenant.
ROLES_PATH = os.environ.get("ROLES_PATH", "/policy-data/roles.json")
_roles_cache: dict = {"mtime": None, "roles": {}}


def _roles() -> dict:
    """roles.json re-read on change so a policy edit + OPA restart is not undone by a stale copy here."""
    try:
        m = os.stat(ROLES_PATH).st_mtime
        if _roles_cache["mtime"] != m:
            with open(ROLES_PATH) as f:
                _roles_cache["roles"] = json.load(f).get("roles", {})
            _roles_cache["mtime"] = m
    except OSError:
        _roles_cache["roles"] = {}  # fail closed: no roles file, no tool access
    return _roles_cache["roles"]


def authorize(authorization: str | None, x_service_token: str | None, tool: str) -> dict:
    span = trace.get_current_span()
    span.set_attribute("authz.tool", tool)
    if x_service_token != SERVICE_TOKEN:
        span.set_attribute("authz.result", "deny_no_service_token")
        raise HTTPException(403, "system-of-record API is reachable only through the agent runtime")
    claims = verify(authorization)
    allowed = _roles().get(claims["role"], {}).get("tools", [])
    if tool not in allowed:
        span.set_attribute("authz.result", "deny_role")
        span.set_attribute("policy.result", "deny")
        span.set_attribute("policy.reason", "tool_not_in_role")
        raise HTTPException(403, "role not permitted for this resource")
    span.set_attribute("authz.result", "allow")
    return claims


@app.get("/api/transactions/{tx_id}/analysis")
def transaction_analysis(tx_id: int, authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None)):
    """Minimized, purpose-built result. Defense in depth: the caller's tenant must own the transaction
    even though OPA already authorized the tool call in the runtime."""
    claims = authorize(authorization, x_service_token, "get_transaction_analysis")
    tx = db.query("SELECT id, tenant_id, amount, currency, result, decline_code, fraud_score FROM transactions WHERE id = %s",
                  (tx_id,), one=True, table="transactions")
    if not tx:
        raise HTTPException(404, "transaction not found")
    if tx["tenant_id"] != claims["tenant_id"]:
        trace.get_current_span().set_attribute("authz.result", "deny_tenant_mismatch")
        raise HTTPException(403, "transaction belongs to another tenant")
    out = {"id": tx["id"], "amount": float(tx["amount"]), "currency": tx["currency"], "result": tx["result"],
           "decline_category": DECLINE_CATEGORY.get(tx["decline_code"] or "", None), "fraud_score": float(tx["fraud_score"])}
    trace.get_current_span().set_attribute("tool.result.fields", sorted(out.keys()))
    return out


def _tx_row(tx: dict) -> dict:
    """Minimized transaction: no tenant echo, no donor reference, decline code mapped to a category. gateway/campaign_id/
    occurred_at are absent (None) on rows selected before the transaction-investigation tool set (Phase 3) added them."""
    occurred_at = tx.get("occurred_at")
    return {"id": tx["id"], "donation_id": tx["donation_id"], "amount": float(tx["amount"]), "currency": tx["currency"],
            "result": tx["result"], "decline_category": DECLINE_CATEGORY.get(tx["decline_code"] or "", None),
            "fraud_score": float(tx["fraud_score"]) if tx["fraud_score"] is not None else None,
            "gateway": tx.get("gateway"), "campaign_id": tx.get("campaign_id"),
            "occurred_at": occurred_at.isoformat() if occurred_at else None, "ip_address": tx.get("ip_address")}


def _donor_summary(donor: dict) -> dict:
    """Donor giving summary for finance staff: donor id + aggregates. No name, phone or email in any form: the output
    guardrails (Presidio + NeMo self-check) treat even a masked address as disclosure, and allow-listing masked shapes
    would hand the model an exfiltration format ("name at domain"). Staff already know the email they searched for."""
    agg = db.query(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount) FILTER (WHERE status = 'approved'), 0) AS total_approved, "
        "(SELECT status FROM donations d2 WHERE d2.donor_id = %s ORDER BY id DESC LIMIT 1) AS last_status "
        "FROM donations WHERE donor_id = %s", (donor["id"], donor["id"]), one=True, table="donations")
    return {"donor_id": donor["id"], "donation_count": int(agg["n"]), "total_approved": float(agg["total_approved"]),
            "currency": "CAD", "last_status": agg["last_status"]}


NOT_FOUND = "not found"  # one message for missing and foreign ids: no cross-tenant existence oracle


@app.get("/api/donations/{donation_id}")
def donation(donation_id: int, authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None)):
    """Donation with its transactions, caller's tenant only. Missing and foreign ids are indistinguishable."""
    claims = authorize(authorization, x_service_token, "get_donation")
    d = db.query("SELECT id, donor_id, amount, currency, status FROM donations WHERE id = %s AND tenant_id = %s",
                 (donation_id, claims["tenant_id"]), one=True, table="donations")
    if not d:
        raise HTTPException(404, NOT_FOUND)
    txs = db.query("SELECT id, donation_id, amount, currency, result, decline_code, fraud_score, gateway, campaign_id, occurred_at "
                   "FROM transactions WHERE donation_id = %s AND tenant_id = %s ORDER BY id", (donation_id, claims["tenant_id"]), table="transactions")
    out = {"id": d["id"], "amount": float(d["amount"]), "currency": d["currency"], "status": d["status"], "donor_id": d["donor_id"],
           "transactions": [_tx_row(t) for t in txs]}
    trace.get_current_span().set_attribute("tool.result.fields", sorted(out.keys()))
    return out


@app.post("/api/donations/{donation_id}/resend-receipt")
def resend_receipt(donation_id: int, authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None)):
    """Phase 10: the one real write-ish action behind the approval workflow. No outbound email capability exists in
    this POC (no SMTP, no mail container) so "resending" is a no-op status-flip, consistent with tax-receipt-policy.md
    framing this as something "reissued by a charity administrator". Missing and foreign ids are indistinguishable."""
    claims = authorize(authorization, x_service_token, "resend_receipt")
    d = db.query("UPDATE donations SET receipt_resent_at = now() WHERE id = %s AND tenant_id = %s RETURNING id, receipt_resent_at",
                 (donation_id, claims["tenant_id"]), one=True, table="donations")
    if not d:
        raise HTTPException(404, NOT_FOUND)
    return {"donation_id": d["id"], "receipt_resent_at": d["receipt_resent_at"].isoformat()}


def _codes_for(decline_category: str) -> list[str]:
    codes = [c for c, cat in DECLINE_CATEGORY.items() if cat == decline_category]
    if not codes:
        raise HTTPException(422, f"unknown decline_category; one of {sorted(set(DECLINE_CATEGORY.values()))}")
    return codes


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, f"invalid time {value!r}; use ISO 8601") from None


def _query_transactions(tenant_id: str, *, result: str | None = None, decline_codes: list[str] | None = None,
                        min_amount: float | None = None, max_amount: float | None = None, campaign_id: int | None = None,
                        gateway: str | None = None, start: datetime | None = None, end: datetime | None = None, limit: int = 20) -> list[dict]:
    """Shared transaction query for list_transactions (narrow filters) and search_transactions (full filter set, Phase 3)."""
    sql = "SELECT id, donation_id, amount, currency, result, decline_code, fraud_score, gateway, campaign_id, occurred_at, ip_address FROM transactions WHERE tenant_id = %s"
    params: list = [tenant_id]
    if result:
        sql += " AND result = %s"; params.append(result)
    if decline_codes:
        sql += " AND decline_code = ANY(%s)"; params.append(decline_codes)
    if min_amount is not None:
        sql += " AND amount >= %s"; params.append(min_amount)
    if max_amount is not None:
        sql += " AND amount <= %s"; params.append(max_amount)
    if campaign_id is not None:
        sql += " AND campaign_id = %s"; params.append(campaign_id)
    if gateway is not None:
        sql += " AND gateway = %s"; params.append(gateway)
    if start is not None:
        sql += " AND occurred_at >= %s"; params.append(start)
    if end is not None:
        sql += " AND occurred_at < %s"; params.append(end)
    sql += " ORDER BY id DESC LIMIT %s"; params.append(limit)
    return db.query(sql, tuple(params), table="transactions")


@app.get("/api/transactions")
def transactions(authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None), result: str | None = None, decline_category: str | None = None,
                 min_amount: float | None = None, max_amount: float | None = None, limit: int = 20):
    """Tenant-scoped transaction list. The tenant is the JWT's, never a parameter."""
    claims = authorize(authorization, x_service_token, "list_transactions")
    if result is not None and result not in ("approved", "declined"):
        raise HTTPException(422, "result must be approved or declined")
    codes = _codes_for(decline_category) if decline_category is not None else None
    limit = max(1, min(int(limit), 50))
    rows = _query_transactions(claims["tenant_id"], result=result, decline_codes=codes, min_amount=min_amount, max_amount=max_amount, limit=limit)
    return {"transactions": [_tx_row(t) for t in rows], "count": len(rows), "limit": limit}


@app.get("/api/transaction-search")
def transaction_search(authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None),
                       result: str | None = None, decline_category: str | None = None, min_amount: float | None = None,
                       max_amount: float | None = None, campaign_id: int | None = None, gateway: str | None = None,
                       start_time: str | None = None, end_time: str | None = None, limit: int = 20):
    """search_transactions (Phase 3): the full filter set -- campaign, gateway and a time window on top of
    list_transactions' narrower filters. A dedicated route, not a shared one, so this endpoint's own authorize() call
    hardcodes its own tool name (same one-route-per-tool invariant every other endpoint here follows)."""
    claims = authorize(authorization, x_service_token, "search_transactions")
    if result is not None and result not in ("approved", "declined"):
        raise HTTPException(422, "result must be approved or declined")
    codes = _codes_for(decline_category) if decline_category is not None else None
    if gateway is not None and gateway not in ("stripe", "adyen"):
        raise HTTPException(422, "unknown gateway; one of stripe, adyen")
    start, end = _parse_time(start_time), _parse_time(end_time)
    limit = max(1, min(int(limit), 50))
    rows = _query_transactions(claims["tenant_id"], result=result, decline_codes=codes, min_amount=min_amount, max_amount=max_amount,
                               campaign_id=campaign_id, gateway=gateway, start=start, end=end, limit=limit)
    return {"transactions": [_tx_row(t) for t in rows], "count": len(rows), "limit": limit}


@app.get("/api/transactions/{tx_id}")
def transaction(tx_id: int, authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None)):
    """get_transaction (Phase 3): the full minimized transaction row by id, caller's tenant only."""
    claims = authorize(authorization, x_service_token, "get_transaction")
    tx = db.query("SELECT id, donation_id, tenant_id, amount, currency, result, decline_code, fraud_score, gateway, campaign_id, occurred_at, ip_address "
                  "FROM transactions WHERE id = %s", (tx_id,), one=True, table="transactions")
    if not tx or tx["tenant_id"] != claims["tenant_id"]:
        raise HTTPException(404, NOT_FOUND)
    return _tx_row(tx)


def _tx_filter_sql(tenant_id: str, *, campaign_id: int | None = None, result: str | None = None,
                   start: datetime | None = None, end: datetime | None = None) -> tuple[str, list]:
    sql = "WHERE tenant_id = %s"
    params: list = [tenant_id]
    if campaign_id is not None:
        sql += " AND campaign_id = %s"; params.append(campaign_id)
    if result is not None:
        sql += " AND result = %s"; params.append(result)
    if start is not None:
        sql += " AND occurred_at >= %s"; params.append(start)
    if end is not None:
        sql += " AND occurred_at < %s"; params.append(end)
    return sql, params


def _transaction_stats(tenant_id: str, *, campaign_id: int | None = None, result: str | None = None,
                       start: datetime | None = None, end: datetime | None = None) -> dict:
    """Shared aggregate for get_campaign_statistics, get_transaction_statistics and compare_transaction_periods
    (Phase 3/5): matches the plan's example tool contract (transaction_count, successful_count, declined_count,
    success_rate, total_amount) exactly. result, when given, scopes every figure to that subset (e.g. "declined")."""
    where, params = _tx_filter_sql(tenant_id, campaign_id=campaign_id, result=result, start=start, end=end)
    agg = db.query(f"SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE result = 'approved') AS ok, "
                   f"COUNT(*) FILTER (WHERE result = 'declined') AS declined, COALESCE(SUM(amount), 0) AS total "
                   f"FROM transactions {where}", tuple(params), one=True, table="transactions")
    n, ok, declined = int(agg["n"]), int(agg["ok"]), int(agg["declined"])
    return {"transaction_count": n, "successful_count": ok, "declined_count": declined,
            "success_rate": round(ok / n, 4) if n else None, "total_amount": float(agg["total"]), "currency": "CAD"}


def _decline_breakdown(tenant_id: str, *, campaign_id: int | None = None, start: datetime | None = None,
                       end: datetime | None = None) -> tuple[dict, int, float]:
    where, params = _tx_filter_sql(tenant_id, campaign_id=campaign_id, result="declined", start=start, end=end)
    rows = db.query(f"SELECT decline_code, COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total FROM transactions {where} GROUP BY decline_code",
                    tuple(params), table="transactions")
    by_cat: dict[str, dict] = {}
    total_n, total_amt = 0, 0.0
    for r in rows:
        k = DECLINE_CATEGORY.get(r["decline_code"] or "", "other")
        n, amt = int(r["n"]), float(r["total"])
        prev = by_cat.get(k, {"count": 0, "total_amount": 0.0})
        by_cat[k] = {"count": prev["count"] + n, "total_amount": round(prev["total_amount"] + amt, 2)}
        total_n += n; total_amt += amt
    return by_cat, total_n, round(total_amt, 2)


_PERIOD_HOURS = {  # period name -> (hours before now, hours before now) marking the window's [start, end)
    "last_hour": (1, 0), "previous_hour": (2, 1), "last_24h": (24, 0), "previous_24h": (48, 24)}


def _period_bounds(period: str) -> tuple[datetime, datetime]:
    if period not in _PERIOD_HOURS:
        raise HTTPException(422, f"unknown period {period!r}; one of {sorted(_PERIOD_HOURS)}")
    start_h, end_h = _PERIOD_HOURS[period]
    now = datetime.now(timezone.utc)
    return now - timedelta(hours=start_h), now - timedelta(hours=end_h)


@app.get("/api/transaction-statistics")
def transaction_statistics(authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None),
                           campaign_id: int | None = None, start_time: str | None = None, end_time: str | None = None, status: str | None = None):
    """get_transaction_statistics (Phase 3), matching the plan's example contract: organization_id is implicit (the
    JWT tenant), campaign_id/start_time/end_time/status are all optional filters."""
    claims = authorize(authorization, x_service_token, "get_transaction_statistics")
    if status is not None and status not in ("approved", "declined"):
        raise HTTPException(422, "status must be approved or declined")
    start, end = _parse_time(start_time), _parse_time(end_time)
    return _transaction_stats(claims["tenant_id"], campaign_id=campaign_id, result=status, start=start, end=end)


@app.get("/api/transaction-comparison")
def transaction_comparison(authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None),
                          campaign_id: int | None = None, current: str = "last_hour", previous: str = "previous_hour"):
    """compare_transaction_periods (Phase 5's worked example): current="last_hour", previous="previous_hour" by default."""
    claims = authorize(authorization, x_service_token, "compare_transaction_periods")
    cur_start, cur_end = _period_bounds(current)
    prev_start, prev_end = _period_bounds(previous)
    cur = _transaction_stats(claims["tenant_id"], campaign_id=campaign_id, start=cur_start, end=cur_end)
    prev = _transaction_stats(claims["tenant_id"], campaign_id=campaign_id, start=prev_start, end=prev_end)
    delta = None
    if cur["success_rate"] is not None and prev["success_rate"] is not None:
        delta = round(cur["success_rate"] - prev["success_rate"], 4)
    return {"current": {"period": current, **cur}, "previous": {"period": previous, **prev}, "success_rate_delta": delta}


@app.get("/api/decline-statistics")
def decline_statistics(authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None),
                       campaign_id: int | None = None, start_time: str | None = None, end_time: str | None = None):
    """get_decline_statistics (Phase 3): decline breakdown by category, optionally scoped to a campaign/time window."""
    claims = authorize(authorization, x_service_token, "get_decline_statistics")
    start, end = _parse_time(start_time), _parse_time(end_time)
    by_cat, total_n, total_amt = _decline_breakdown(claims["tenant_id"], campaign_id=campaign_id, start=start, end=end)
    return {"declined_count": total_n, "declined_total": total_amt, "currency": "CAD", "decline_categories": by_cat}


@app.get("/api/gateway-statistics")
def gateway_statistics(authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None),
                       campaign_id: int | None = None, start_time: str | None = None, end_time: str | None = None):
    """get_payment_gateway_statistics (Phase 3): per-gateway transaction/decline counts, the tool the plan's worked
    example (Phase 5) uses to localize a decline spike to one gateway."""
    claims = authorize(authorization, x_service_token, "get_payment_gateway_statistics")
    start, end = _parse_time(start_time), _parse_time(end_time)
    where, params = _tx_filter_sql(claims["tenant_id"], campaign_id=campaign_id, start=start, end=end)
    rows = db.query(f"SELECT gateway, COUNT(*) AS n, COUNT(*) FILTER (WHERE result = 'declined') AS declined, "
                    f"COALESCE(SUM(amount), 0) AS total FROM transactions {where} GROUP BY gateway", tuple(params), table="transactions")
    gateways = {}
    for r in rows:
        n, declined = int(r["n"]), int(r["declined"])
        gateways[r["gateway"]] = {"transaction_count": n, "declined_count": declined,
                                  "decline_rate": round(declined / n, 4) if n else None, "total_amount": float(r["total"])}
    return {"gateways": gateways, "currency": "CAD"}


@app.get("/api/fraud-signals")
def fraud_signals(authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None),
                  campaign_id: int | None = None, start_time: str | None = None, end_time: str | None = None,
                  min_fraud_score: float = 0.5, limit: int = 20):
    """get_fraud_signals (Phase 3): transactions at or above a fraud-score threshold, with an aggregate so the agent
    can tell "no significant fraud activity" apart from a real card-testing pattern (Phase 5's worked example)."""
    claims = authorize(authorization, x_service_token, "get_fraud_signals")
    start, end = _parse_time(start_time), _parse_time(end_time)
    where, params = _tx_filter_sql(claims["tenant_id"], campaign_id=campaign_id, start=start, end=end)
    where += " AND fraud_score >= %s"; params.append(min_fraud_score)
    limit = max(1, min(int(limit), 50))
    rows = db.query(f"SELECT id, donation_id, amount, currency, result, decline_code, fraud_score, gateway, campaign_id, occurred_at, ip_address "
                    f"FROM transactions {where} ORDER BY fraud_score DESC LIMIT %s", tuple(params) + (limit,), table="transactions")
    flagged = [_tx_row(r) for r in rows]
    avg = round(sum(t["fraud_score"] for t in flagged) / len(flagged), 4) if flagged else None
    return {"flagged_count": len(flagged), "average_fraud_score": avg, "min_fraud_score": min_fraud_score, "transactions": flagged}


@app.get("/api/application-errors")
def application_errors(authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None),
                       service: str | None = None, start_time: str | None = None, end_time: str | None = None, limit: int = 20):
    """get_application_errors (Phase 3): tenant-scoped app error log, newest first."""
    claims = authorize(authorization, x_service_token, "get_application_errors")
    start, end = _parse_time(start_time), _parse_time(end_time)
    sql = "SELECT id, service, error_type, message, occurred_at FROM application_errors WHERE tenant_id = %s"
    params: list = [claims["tenant_id"]]
    if service is not None:
        sql += " AND service = %s"; params.append(service)
    if start is not None:
        sql += " AND occurred_at >= %s"; params.append(start)
    if end is not None:
        sql += " AND occurred_at < %s"; params.append(end)
    limit = max(1, min(int(limit), 50))
    sql += " ORDER BY occurred_at DESC LIMIT %s"; params.append(limit)
    rows = db.query(sql, tuple(params), table="application_errors")
    return {"errors": [{"id": r["id"], "service": r["service"], "error_type": r["error_type"], "message": r["message"],
                        "occurred_at": r["occurred_at"].isoformat()} for r in rows], "count": len(rows)}


@app.get("/api/incidents")
def incident_history(authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None),
                     status: str | None = None, start_time: str | None = None, end_time: str | None = None, limit: int = 20):
    """get_incident_history (Phase 3): tenant-scoped incident log, newest first."""
    claims = authorize(authorization, x_service_token, "get_incident_history")
    if status is not None and status not in ("open", "resolved"):
        raise HTTPException(422, "status must be open or resolved")
    start, end = _parse_time(start_time), _parse_time(end_time)
    sql = "SELECT id, title, status, severity, summary, started_at, resolved_at FROM incidents WHERE tenant_id = %s"
    params: list = [claims["tenant_id"]]
    if status is not None:
        sql += " AND status = %s"; params.append(status)
    if start is not None:
        sql += " AND started_at >= %s"; params.append(start)
    if end is not None:
        sql += " AND started_at < %s"; params.append(end)
    limit = max(1, min(int(limit), 50))
    sql += " ORDER BY started_at DESC LIMIT %s"; params.append(limit)
    rows = db.query(sql, tuple(params), table="incidents")
    return {"incidents": [{"id": r["id"], "title": r["title"], "status": r["status"], "severity": r["severity"],
                           "summary": r["summary"], "started_at": r["started_at"].isoformat(),
                           "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None} for r in rows],
           "count": len(rows)}


# ---------- controlled actions (plan Phase 14) ----------
# Graduated autonomy (Phase 9): restart_worker/clear_failed_job/block_ip_temporarily are action_risk=medium,
# create_incident/send_notification/collect_diagnostic_bundle are low -- OPA's tool stage auto-executes both tiers
# once role/tenant checks pass (see policy/authz.rego), unlike resend_receipt/high which always needs an approval.
# No outbound system exists in this POC (no job queue, no WAF, no pager) so each handler is a state-changing no-op --
# same framing resend_receipt uses -- audited in `remediation_actions` rather than silently discarded.
def _log_action(tenant_id: str, action: str, params: dict, performed_by: str, result: dict) -> None:
    db.query("INSERT INTO remediation_actions (tenant_id, action, params, performed_by, result) VALUES (%s,%s,%s::jsonb,%s,%s::jsonb)",
             (tenant_id, action, json.dumps(params), performed_by, json.dumps(result)), table="remediation_actions")


class RestartWorkerReq(BaseModel):
    worker: str = Field(min_length=1, max_length=100)


@app.post("/api/workers/restart")
def restart_worker(req: RestartWorkerReq, authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None)):
    """restart_worker (Phase 14): no real worker fleet in this POC -- a no-op status-flip, consistent with
    resend_receipt. Logged to remediation_actions for audit."""
    claims = authorize(authorization, x_service_token, "restart_worker")
    result = {"worker": req.worker, "restarted": True, "restarted_at": datetime.now(timezone.utc).isoformat()}
    _log_action(claims["tenant_id"], "restart_worker", {"worker": req.worker}, claims["sub"], result)
    return result


class ClearFailedJobReq(BaseModel):
    job_id: str = Field(min_length=1, max_length=100)


@app.post("/api/jobs/clear")
def clear_failed_job(req: ClearFailedJobReq, authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None)):
    """clear_failed_job (Phase 14): no real job queue in this POC -- a no-op status-flip."""
    claims = authorize(authorization, x_service_token, "clear_failed_job")
    result = {"job_id": req.job_id, "cleared": True, "cleared_at": datetime.now(timezone.utc).isoformat()}
    _log_action(claims["tenant_id"], "clear_failed_job", {"job_id": req.job_id}, claims["sub"], result)
    return result


class BlockIpReq(BaseModel):
    ip: str
    duration_minutes: int = Field(default=120, ge=1, le=1440)


@app.post("/api/security/block-ip")
def block_ip_temporarily(req: BlockIpReq, authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None)):
    """block_ip_temporarily (Phase 14): the plan's card-testing mitigation example (Phase 5/9/10) -- no real WAF in
    this POC, so this is a no-op status-flip, time-bounded by construction (duration_minutes, not permanent)."""
    claims = authorize(authorization, x_service_token, "block_ip_temporarily")
    try:
        ipaddress.ip_address(req.ip)
    except ValueError:
        raise HTTPException(422, "ip must be a valid IPv4 or IPv6 address") from None
    blocked_until = datetime.now(timezone.utc) + timedelta(minutes=req.duration_minutes)
    result = {"ip": req.ip, "duration_minutes": req.duration_minutes, "blocked_until": blocked_until.isoformat()}
    _log_action(claims["tenant_id"], "block_ip_temporarily", {"ip": req.ip, "duration_minutes": req.duration_minutes}, claims["sub"], result)
    return result


SEVERITIES = {"low", "medium", "high", "critical"}


class CreateIncidentReq(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    severity: str
    summary: str = Field(min_length=1, max_length=2000)


@app.post("/api/incidents")
def create_incident(req: CreateIncidentReq, authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None)):
    """create_incident (Phase 14): a real row in the same `incidents` table get_incident_history reads -- unlike the
    other five actions, this one has actual tenant-scoped state to show for it."""
    claims = authorize(authorization, x_service_token, "create_incident")
    if req.severity not in SEVERITIES:
        raise HTTPException(422, f"severity must be one of {sorted(SEVERITIES)}")
    row = db.query(
        "INSERT INTO incidents (tenant_id, title, status, severity, summary, started_at) VALUES (%s,%s,'open',%s,%s,now()) "
        "RETURNING id, title, status, severity, summary, started_at",
        (claims["tenant_id"], req.title, req.severity, req.summary), one=True, table="incidents")
    out = {"id": row["id"], "title": row["title"], "status": row["status"], "severity": row["severity"],
           "summary": row["summary"], "started_at": row["started_at"].isoformat()}
    _log_action(claims["tenant_id"], "create_incident", req.model_dump(), claims["sub"], out)
    return out


NOTIFICATION_CHANNELS = {"slack", "email", "pagerduty"}


class SendNotificationReq(BaseModel):
    channel: str
    message: str = Field(min_length=1, max_length=2000)


@app.post("/api/notifications")
def send_notification(req: SendNotificationReq, authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None)):
    """send_notification (Phase 14): no SMTP/Slack/pager integration in this POC -- a no-op send, logged for audit
    (what the agent told a human, and when)."""
    claims = authorize(authorization, x_service_token, "send_notification")
    if req.channel not in NOTIFICATION_CHANNELS:
        raise HTTPException(422, f"channel must be one of {sorted(NOTIFICATION_CHANNELS)}")
    notification_id = f"NTF-{uuid.uuid4().hex[:12]}"
    result = {"sent": True, "channel": req.channel, "notification_id": notification_id}
    _log_action(claims["tenant_id"], "send_notification", req.model_dump(), claims["sub"], result)
    return result


class CollectDiagnosticsReq(BaseModel):
    service: str | None = None
    start_time: str | None = None
    end_time: str | None = None


@app.post("/api/diagnostics/collect")
def collect_diagnostic_bundle(req: CollectDiagnosticsReq, authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None)):
    """collect_diagnostic_bundle (Phase 14): summarizes the same tenant-scoped application_errors/incidents tables
    get_application_errors/get_incident_history read, as a single bundle an agent can attach to an incident or hand
    to on-call -- no log-aggregator export exists in this POC, so the "bundle" is this summary plus an id."""
    claims = authorize(authorization, x_service_token, "collect_diagnostic_bundle")
    start, end = _parse_time(req.start_time), _parse_time(req.end_time)
    err_sql = "SELECT COUNT(*) AS n FROM application_errors WHERE tenant_id = %s"
    err_params: list = [claims["tenant_id"]]
    if req.service is not None:
        err_sql += " AND service = %s"; err_params.append(req.service)
    if start is not None:
        err_sql += " AND occurred_at >= %s"; err_params.append(start)
    if end is not None:
        err_sql += " AND occurred_at < %s"; err_params.append(end)
    error_count = int(db.query(err_sql, tuple(err_params), one=True, table="application_errors")["n"])
    inc_sql = "SELECT COUNT(*) AS n FROM incidents WHERE tenant_id = %s AND status = 'open'"
    open_incidents = int(db.query(inc_sql, (claims["tenant_id"],), one=True, table="incidents")["n"])
    bundle_id = f"DIAG-{uuid.uuid4().hex[:12]}"
    result = {"bundle_id": bundle_id, "service": req.service, "error_count": error_count, "open_incidents": open_incidents,
              "collected_at": datetime.now(timezone.utc).isoformat()}
    _log_action(claims["tenant_id"], "collect_diagnostic_bundle", req.model_dump(), claims["sub"], result)
    return result


@app.get("/api/campaigns/{campaign_id}")
def campaign(campaign_id: int, authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None)):
    """get_campaign (Phase 3): campaign by id, caller's tenant only. Missing and foreign ids are indistinguishable."""
    claims = authorize(authorization, x_service_token, "get_campaign")
    c = db.query("SELECT id, name, status, goal_amount FROM campaigns WHERE id = %s AND tenant_id = %s",
                (campaign_id, claims["tenant_id"]), one=True, table="campaigns")
    if not c:
        raise HTTPException(404, NOT_FOUND)
    return {"id": c["id"], "name": c["name"], "status": c["status"], "goal_amount": float(c["goal_amount"])}


@app.get("/api/campaigns/{campaign_id}/statistics")
def campaign_statistics(campaign_id: int, authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None)):
    """get_campaign_statistics (Phase 3): transaction aggregates + decline breakdown scoped to one campaign."""
    claims = authorize(authorization, x_service_token, "get_campaign_statistics")
    c = db.query("SELECT id FROM campaigns WHERE id = %s AND tenant_id = %s", (campaign_id, claims["tenant_id"]), one=True, table="campaigns")
    if not c:
        raise HTTPException(404, NOT_FOUND)
    stats = _transaction_stats(claims["tenant_id"], campaign_id=campaign_id)
    by_cat, _, _ = _decline_breakdown(claims["tenant_id"], campaign_id=campaign_id)
    return {**stats, "decline_categories": by_cat}


class DonorLookupReq(BaseModel):
    email: str


@app.post("/api/donors/lookup")
def donor_lookup(req: DonorLookupReq, authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None)):
    """POST, not GET: the email travels in the body so it never lands in an http.url / url.query span attribute."""
    claims = authorize(authorization, x_service_token, "find_donor")
    donor = db.query("SELECT id, email FROM donors WHERE tenant_id = %s AND lower(email) = lower(%s)",
                     (claims["tenant_id"], req.email.strip()), one=True, table="donors")
    if not donor:
        raise HTTPException(404, NOT_FOUND)
    return _donor_summary(donor)


@app.get("/api/donors/{donor_id}/profile")
def donor_profile(donor_id: int, authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None)):
    claims = authorize(authorization, x_service_token, "get_donor_profile")
    donor = db.query("SELECT id, email FROM donors WHERE id = %s AND tenant_id = %s", (donor_id, claims["tenant_id"]), one=True, table="donors")
    if not donor:
        raise HTTPException(404, NOT_FOUND)
    return _donor_summary(donor)


@app.get("/api/summary")
def summary(authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None)):
    """Tenant aggregates. Pending donations (no settled transaction yet) are excluded so counts reconcile."""
    claims = authorize(authorization, x_service_token, "get_tenant_donation_summary")
    agg = db.query(
        "SELECT COUNT(*) FILTER (WHERE status = 'approved') AS approved_count, COUNT(*) FILTER (WHERE status = 'declined') AS declined_count, "
        "COALESCE(SUM(amount) FILTER (WHERE status = 'approved'), 0) AS approved_total, "
        "COALESCE(SUM(amount) FILTER (WHERE status = 'declined'), 0) AS declined_total "
        "FROM donations WHERE tenant_id = %s", (claims["tenant_id"],), one=True, table="donations")
    cats = db.query("SELECT t.decline_code, COUNT(*) AS n FROM transactions t JOIN donations d ON d.id = t.donation_id "
                    "WHERE d.tenant_id = %s AND d.status = 'declined' AND t.result = 'declined' GROUP BY t.decline_code",
                    (claims["tenant_id"],), table="transactions")
    by_cat: dict[str, int] = {}
    for c in cats:
        k = DECLINE_CATEGORY.get(c["decline_code"] or "", "other")
        by_cat[k] = by_cat.get(k, 0) + int(c["n"])
    return {"donation_count": int(agg["approved_count"]) + int(agg["declined_count"]), "approved_count": int(agg["approved_count"]),
            "declined_count": int(agg["declined_count"]), "approved_total": float(agg["approved_total"]),
            "declined_total": float(agg["declined_total"]), "currency": "CAD", "decline_categories": by_cat}


@app.get("/api/me/donations")
def my_donations(authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None)):
    """Donor self-service: donations whose donor email equals the JWT subject, within the JWT tenant."""
    claims = authorize(authorization, x_service_token, "get_my_donations")
    rows = db.query("SELECT d.id, d.amount, d.currency, d.status FROM donations d JOIN donors r ON r.id = d.donor_id "
                    "WHERE d.tenant_id = %s AND r.tenant_id = %s AND lower(r.email) = lower(%s) ORDER BY d.id DESC",
                    (claims["tenant_id"], claims["tenant_id"], claims["sub"]), table="donations")
    return {"donations": [{"id": r["id"], "amount": float(r["amount"]), "currency": r["currency"], "status": r["status"]} for r in rows]}


# ---------- Policy Information Points (service token only; return the owning tenant, nothing else) ----------
PIP_TABLES = {"transactions": "transactions", "donations": "donations", "donors": "donors", "campaigns": "campaigns"}


def _pip(table: str, row_id: int, x_service_token: str | None):
    if x_service_token != SERVICE_TOKEN:
        raise HTTPException(401, "service token required")
    row = db.query(f"SELECT tenant_id FROM {PIP_TABLES[table]} WHERE id = %s", (row_id,), one=True, table=PIP_TABLES[table])  # noqa: S608 (table from fixed map)
    if not row:
        raise HTTPException(404, NOT_FOUND)
    return {"tenant_id": row["tenant_id"]}


@app.get("/internal/transactions/{tx_id}/tenant")
def transaction_tenant(tx_id: int, x_service_token: str | None = Header(default=None)):
    """Policy Information Point for the runtime: returns only the owning tenant. Service-token only;
    not part of the user-facing surface."""
    return _pip("transactions", tx_id, x_service_token)


@app.get("/internal/donations/{donation_id}/tenant")
def donation_tenant(donation_id: int, x_service_token: str | None = Header(default=None)):
    return _pip("donations", donation_id, x_service_token)


@app.get("/internal/donors/{donor_id}/tenant")
def donor_tenant(donor_id: int, x_service_token: str | None = Header(default=None)):
    return _pip("donors", donor_id, x_service_token)


@app.get("/internal/campaigns/{campaign_id}/tenant")
def campaign_tenant(campaign_id: int, x_service_token: str | None = Header(default=None)):
    return _pip("campaigns", campaign_id, x_service_token)


# ---------- demo chat console (static, same origin) ----------
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")


@app.get("/ui")
def ui():
    return FileResponse(os.path.join(UI_DIR, "index.html"), media_type="text/html")


@app.get("/health")
def health():
    return {"ok": True}
