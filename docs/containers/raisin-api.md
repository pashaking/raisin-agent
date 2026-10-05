# raisin-api

The application. Plays three roles from the design (diagram 01): **Identity** (Entra ID stand-in), **Application APIs**
(the public donation form and the two AI entry points) and **Backend + system of record** (donations, transactions, donors,
the AI registry table). It is the only service that talks to the `raisin` database and the only service end users talk to.

| | |
|---|---|
| Build | `services/raisin-api/Dockerfile` (FastAPI, uvicorn, psycopg, PyJWT, cryptography) |
| Port | 18080 → 8080 |
| Depends on | `postgres` healthy, `payment-gateway` started |
| Health | `GET /health` → `{"ok": true}` |
| Code | `services/raisin-api/app.py` (~800 lines), `db.py`, `keys.py`, `ui/index.html` |

## Configuration

| Env | Purpose |
|---|---|
| `RAISIN_DATABASE_URL` | `postgresql://poc:...@postgres:5432/raisin` |
| `AGENT_RUNTIME_URL` | `http://agent-runtime:8090`, target of `/assistant` and `/story` forwards |
| `PAYMENT_GATEWAY_URL` | `http://payment-gateway:8070` |
| `RUNTIME_SERVICE_TOKEN` | shared secret; required on `/api/*` and `/internal/*` (`X-Service-Token`) |
| `ROLES_PATH` | `/policy-data/roles.json`, the same file OPA loads, bind-mounted read-only; re-read on mtime change |
| `JWT_PRIVATE_KEY_PEM` | optional; otherwise a fresh RS256 keypair is generated at boot and the `kid` is a hash of the public key, so a restart rotates the key and verifiers refetch the JWKS |

## Interface

### Identity

| Endpoint | Auth | Behaviour |
|---|---|---|
| `POST /auth/token {user}` | none (demo) | looks the email up in `users`, mints RS256 JWT: `iss=raisin-api`, `aud=aicp`, `sub=email`, `tenant_id`, `role`, `exp=+900s`, `jti` |
| `GET /auth/users` | none (demo) | seeded users for the console's identity picker |
| `GET /.well-known/jwks.json` | none | public key set the runtime verifies against |

### Payment path

| Endpoint | Auth | Behaviour |
|---|---|---|
| `POST /donate` | none (public form) | body `{tenant_id, amount>0, currency, payment_token, donor{email, phone?, name?}}`, `extra=forbid`; 404 unknown tenant; donor upsert never overwrites existing phone/name; id from `donation_id_seq`; INSERT-only rows; forwards `{amount, currency, payment_token}` to the gateway; span gets `tenant_id`, `donation.id`, `payment.result`, never donor fields |

### AI path

| Endpoint | Auth | Behaviour |
|---|---|---|
| `POST /assistant {question, model?, temperature?, session_id?}` | Bearer JWT | verifies locally, forwards to `agent-runtime /run` with the same `Authorization` header; copies status code; adds `X-Trace-Id` header and `trace_id` field |
| `POST /story {free_text, model?}` | Bearer JWT | same, to `/story` |
| `GET /ui` | none | static chat console (same origin) |

### System of record (tools call these)

All require `X-Service-Token` AND a valid JWT AND the JWT role to include the backing tool (per `roles.json`) AND filter by
the JWT tenant in SQL. Four checks; OPA in the runtime is a fifth, independent one.

| Endpoint | Backing tool | Returns |
|---|---|---|
| `GET /api/transactions/{id}/analysis` | `get_transaction_analysis` | `{id, amount, currency, result, decline_category, fraud_score}`; 403 if another tenant's (span `authz.result=deny_tenant_mismatch`) |
| `GET /api/donations/{id}` | `get_donation` | `{id, amount, currency, status, donor_id, transactions[]}`; 404 `not found` for missing **and** foreign ids |
| `GET /api/transactions?result&decline_category&min_amount&max_amount&limit` | `list_transactions` | `{transactions[], count, limit}`; limit clamped 1..50; 422 on bad enum |
| `POST /api/donors/lookup {email}` | `find_donor` | `{donor_id, donation_count, total_approved, currency, last_status}`; POST so the email never lands in a URL attribute; no name/email/phone in the result |
| `GET /api/donors/{id}/profile` | `get_donor_profile` | same summary shape |
| `GET /api/summary` | `get_tenant_donation_summary` | counts, totals, `decline_categories`; pending donations excluded |
| `GET /api/me/donations` | `get_my_donations` | donations joined on `donors.email = JWT sub` within the JWT tenant |
| `POST /api/donations/{id}/resend-receipt` | `resend_receipt` | `{donation_id, receipt_resent_at}`; no-op status-flip (`UPDATE ... SET receipt_resent_at = now()`) -- no outbound email capability exists anywhere in this POC; 404 `not found` for missing/foreign ids. Called only from `tools.execute_approved`, after a human approval (Phase 10), never from the normal tool-call path -- OPA's tool stage denies `resend_receipt` with `requires_approval` before this is ever reached that way |

Transaction-investigation tool set (implementation plan Phase 3, literal plan tool names). `campaigns`, `application_errors`
and `incidents` are tenant-scoped, seed-owned tables (`services/seed/fixtures.py` `CAMPAIGNS`/`CAMPAIGN_TRANSACTIONS`/
`APPLICATION_ERRORS`/`INCIDENTS`); `transactions` gained `campaign_id`, `gateway`, `occurred_at`. All action_risk=low
(read-only; Phase 1 autonomy is READ+ANALYZE+RECOMMEND only):

| Endpoint | Backing tool | Returns |
|---|---|---|
| `GET /api/campaigns/{id}` | `get_campaign` | `{id, name, status, goal_amount}`; 404 for missing/foreign ids |
| `GET /api/campaigns/{id}/statistics` | `get_campaign_statistics` | transaction aggregates + `decline_categories` for that campaign (all-time) |
| `GET /api/transactions/{id}` | `get_transaction` | full minimized transaction incl. `gateway`, `campaign_id`, `occurred_at` |
| `GET /api/transaction-search?result&decline_category&campaign_id&gateway&min_amount&max_amount&start_time&end_time&limit` | `search_transactions` | `list_transactions`'s full-filter sibling (separate route so each keeps its own hardcoded `authorize()` tool name) |
| `GET /api/transaction-statistics?campaign_id&start_time&end_time&status` | `get_transaction_statistics` | `{transaction_count, successful_count, declined_count, success_rate, total_amount}` -- the plan's Phase 3 example contract verbatim |
| `GET /api/transaction-comparison?campaign_id&current&previous` | `compare_transaction_periods` | two of the above (`current`/`previous` one of `last_hour`/`previous_hour`/`last_24h`/`previous_24h`) + `success_rate_delta` |
| `GET /api/decline-statistics?campaign_id&start_time&end_time` | `get_decline_statistics` | `declined_count`, `declined_total`, `decline_categories` |
| `GET /api/gateway-statistics?campaign_id&start_time&end_time` | `get_payment_gateway_statistics` | per-`gateway` transaction/decline counts and decline rate |
| `GET /api/fraud-signals?campaign_id&start_time&end_time&min_fraud_score&limit` | `get_fraud_signals` | transactions at/above `min_fraud_score` (default 0.5) + `flagged_count`/`average_fraud_score` |
| `GET /api/application-errors?service&start_time&end_time&limit` | `get_application_errors` | tenant-scoped app error log, newest first |
| `GET /api/incidents?status&start_time&end_time&limit` | `get_incident_history` | tenant-scoped incident log, newest first |

Controlled actions (implementation plan Phase 14). `restart_worker`/`clear_failed_job`/`block_ip_temporarily` are
`action_risk=medium`, the other three `low`; OPA's tool stage auto-executes both tiers like any other tool once
role/tenant checks pass (graduated autonomy, Phase 9) -- unlike `resend_receipt`/`high`, none of these ever reaches
the approval workflow. No worker fleet, job queue, WAF or pager exists in this POC, so each is a no-op status-flip,
audited to `remediation_actions` (`tenant_id, action, params, performed_by, result, created_at`); `create_incident`
is the one exception with real state -- it inserts into the same `incidents` table `get_incident_history` reads.
`transactions.ip_address` (nullable, only set on campaign 2's card-testing cluster fixture) is what
`block_ip_temporarily` acts on, surfaced through `get_fraud_signals`/`get_transaction`/`search_transactions`.

| Endpoint | Backing tool | Returns |
|---|---|---|
| `POST /api/workers/restart {worker}` | `restart_worker` | `{worker, restarted, restarted_at}` |
| `POST /api/jobs/clear {job_id}` | `clear_failed_job` | `{job_id, cleared, cleared_at}` |
| `POST /api/security/block-ip {ip, duration_minutes?}` | `block_ip_temporarily` | `{ip, duration_minutes, blocked_until}`; 422 on an invalid `ip` |
| `POST /api/incidents {title, severity, summary}` | `create_incident` | the new `incidents` row (`status="open"`); 422 on an unknown `severity` |
| `POST /api/notifications {channel, message}` | `send_notification` | `{sent, channel, notification_id}`; 422 on an unknown `channel` |
| `POST /api/diagnostics/collect {service?, start_time?, end_time?}` | `collect_diagnostic_bundle` | `{bundle_id, service, error_count, open_incidents, collected_at}` |

### Policy Information Points (service token only)

`GET /internal/transactions/{id}/tenant`, `/internal/donations/{id}/tenant`, `/internal/donors/{id}/tenant`,
`/internal/campaigns/{id}/tenant` → `{tenant_id}` or 404.
Used by the runtime before the OPA tool-stage decision so OPA can compare the resource's tenant with the caller's.

## Spans

FastAPI server spans per route; `db.query` per statement; `enduser.id` / `tenant_id` / `role` after JWT verification;
`authz.tool` / `authz.result` on `/api/*`; on a role deny also `policy.result=deny` + `policy.reason=tool_not_in_role`
(so it reaches the SIEM feed); `tool.result.fields` listing returned keys.

## When it is down

Everything stops: no tokens, no `/donate`, no AI entry point, no tool backend. The runtime's tool calls to `/api/*` raise
`RuntimeError("raisin-api ...")` and surface as 502 `gateway_error`; the PIP call failing has the same effect.

## Success example

Scenario 1 backend call, visible in trace `71343ded243c0f5bda29fde82d97bf74`:

```
raisin-api  GET /api/donations/{id}   authz.tool=get_donation  authz.result=allow  enduser.id=finance.a@charity-a.test  tenant_id=tenant-a  role=Finance
  db.query  (donations WHERE id = 873928 AND tenant_id = 'tenant-a')
  db.query  (transactions WHERE donation_id = 873928 AND tenant_id = 'tenant-a')
  tool.result.fields = [amount, currency, donor_id, id, status, transactions]
```

The row that came back to the runtime (and then to the model):
`{"id": 873928, "amount": 250.0, "currency": "CAD", "status": "declined", "donor_id": ..., "transactions": [{"id": 873928, "result": "declined", "decline_category": "insufficient_funds", "fraud_score": 0.12, ...}]}`.
No donor name, email or phone; decline code 51 already mapped to a category.

## Failure examples

Direct call without the service token (F4):

```
GET /api/donations/873928  Authorization: Bearer <finance.a JWT>
HTTP 403 {"detail": "system-of-record API is reachable only through the agent runtime"}
```

Donor JWT on a finance route with the service token (F5):

```
GET /api/summary  Authorization: Bearer <donor.b JWT>  X-Service-Token: <token>
HTTP 403 {"detail": "role not permitted for this resource"}
SIEM: c8f58ce69712de9e  GET /api/summary  {"enduser.id": "donor.b@example.test", "role": "Donor", "policy.result": "deny", "policy.reason": "tool_not_in_role"}
```

Client-chosen donation id on the public form (F7):

```
POST /donate {"donation_id": 1, ...}
HTTP 422 {"detail": [{"type": "extra_forbidden", "loc": ["body", "donation_id"], "msg": "Extra inputs are not permitted", "input": 1}]}
```

## Related

- Design premise for the identity swap (Dex / Entra ID): `../../../02_Donation_AI_Control_Plane_POC_Design.md`
- Security findings F2 (donate integrity), F6 (uniform not_found), F12 (service token + role re-check): `../../../03_AI_Control_Plane_Security_Review.md`
- Callers: [agent-runtime.md](agent-runtime.md); data: [postgres.md](postgres.md)
