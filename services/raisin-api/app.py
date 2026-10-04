"""raisin-api: identity route (JWT + JWKS), application API, and system-of-record API.

Diagram-01 boxes it covers: Identity (Entra ID stand-in), Application APIs, Backend services + RDS.
The payment path (/donate -> payment-gateway) never touches the AI path (/assistant, /story -> agent-runtime).
"""
import json
import os
import time
import uuid

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

DECLINE_CATEGORY = {"51": "insufficient_funds", "05": "do_not_honor", "14": "invalid_card", "54": "expired_card"}


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
    """Minimized transaction: no tenant echo, no donor reference, decline code mapped to a category."""
    return {"id": tx["id"], "donation_id": tx["donation_id"], "amount": float(tx["amount"]), "currency": tx["currency"],
            "result": tx["result"], "decline_category": DECLINE_CATEGORY.get(tx["decline_code"] or "", None),
            "fraud_score": float(tx["fraud_score"]) if tx["fraud_score"] is not None else None}


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
    txs = db.query("SELECT id, donation_id, amount, currency, result, decline_code, fraud_score FROM transactions "
                   "WHERE donation_id = %s AND tenant_id = %s ORDER BY id", (donation_id, claims["tenant_id"]), table="transactions")
    out = {"id": d["id"], "amount": float(d["amount"]), "currency": d["currency"], "status": d["status"], "donor_id": d["donor_id"],
           "transactions": [_tx_row(t) for t in txs]}
    trace.get_current_span().set_attribute("tool.result.fields", sorted(out.keys()))
    return out


@app.get("/api/transactions")
def transactions(authorization: str | None = Header(default=None), x_service_token: str | None = Header(default=None), result: str | None = None, decline_category: str | None = None,
                 min_amount: float | None = None, max_amount: float | None = None, limit: int = 20):
    """Tenant-scoped transaction list. The tenant is the JWT's, never a parameter."""
    claims = authorize(authorization, x_service_token, "list_transactions")
    if result is not None and result not in ("approved", "declined"):
        raise HTTPException(422, "result must be approved or declined")
    codes = None
    if decline_category is not None:
        codes = [c for c, cat in DECLINE_CATEGORY.items() if cat == decline_category]
        if not codes:
            raise HTTPException(422, f"unknown decline_category; one of {sorted(set(DECLINE_CATEGORY.values()))}")
    limit = max(1, min(int(limit), 50))
    sql = "SELECT id, donation_id, amount, currency, result, decline_code, fraud_score FROM transactions WHERE tenant_id = %s"
    params: list = [claims["tenant_id"]]
    if result:
        sql += " AND result = %s"; params.append(result)
    if codes:
        sql += " AND decline_code = ANY(%s)"; params.append(codes)
    if min_amount is not None:
        sql += " AND amount >= %s"; params.append(min_amount)
    if max_amount is not None:
        sql += " AND amount <= %s"; params.append(max_amount)
    sql += " ORDER BY id DESC LIMIT %s"; params.append(limit)
    rows = db.query(sql, tuple(params), table="transactions")
    return {"transactions": [_tx_row(t) for t in rows], "count": len(rows), "limit": limit}


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
PIP_TABLES = {"transactions": "transactions", "donations": "donations", "donors": "donors"}


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


# ---------- demo chat console (static, same origin) ----------
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")


@app.get("/ui")
def ui():
    return FileResponse(os.path.join(UI_DIR, "index.html"), media_type="text/html")


@app.get("/health")
def health():
    return {"ok": True}
