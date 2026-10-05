# agent-runtime

The half of the **AI Gateway** box that LiteLLM does not do: the tool loop, the Policy Enforcement Point (PEP), the
guardrail client, and the place where PII placeholders live and die. Roughly 1250 lines of Python across ten modules. It
holds no database credentials for the system of record; it holds LiteLLM virtual keys, an OPA token, a guardrails token
and a service token for `raisin-api`.

| | |
|---|---|
| Build | `services/agent-runtime/Dockerfile` (FastAPI, openai SDK, httpx, psycopg, PyJWT) |
| Port | 18090 → 8090 (the console and demo go through `raisin-api`, not this port) |
| Depends on | `litellm` healthy, `raisin-api` healthy, `opa` started |
| Health | `GET /health` → `{"ok": true, "trace_content": "off", "debug_panel": "on"}` |
| Code | `app.py` (endpoints, envelope), `policy.py` (PEP), `guardrails.py` (Presidio + guardrails client, stages), `tools.py` (12 handlers, PIP, placeholder resolution, RAG), `memory.py` (controlled agent memory, kb database), `approvals.py` (human approval queue, kb database), `llm.py` (chat/embed spans, content minimization), `auth.py` (JWKS verification), `limits.py` (loop-pattern detector, termination messages — pure, no deps), `config.py` |

## Configuration

| Env | Purpose |
|---|---|
| `LITELLM_BASE_URL` | `http://litellm:4000` |
| `LITELLM_KEY_TENANT_A_FINANCE`, `LITELLM_KEY_TENANT_B_FINANCE`, `LITELLM_KEY_TENANT_C_FINANCE`, `LITELLM_KEY_STORY_GENERATOR` | virtual keys; mapped in `config.GATEWAY_KEYS` by `(tenant, role)`: A/Finance, B/Finance, B/Donor (shares tenant B's budget), C/Finance, C/Donor (shares tenant C's budget), A/Participant. No mapping → 403 `no_gateway_key` |
| `OPA_URL`, `OPA_TOKEN_RUNTIME` | PDP; the token may only `POST /v1/data/aicp/authz/decision` (OPA `system.authz`) |
| `GUARDRAILS_URL`, `GUARDRAILS_AUTH_TOKEN` | orchestrator |
| `PRESIDIO_ANALYZER_URL`, `PRESIDIO_ANONYMIZER_URL` | PII services |
| `RAISIN_API_URL`, `RUNTIME_SERVICE_TOKEN` | tool backend + PIPs; the token is what makes `/api/*` reachable |
| `KB_DATABASE_URL` | pgvector `kb` database (the only DB the runtime touches) |
| `DEBUG_TRACE_CONTENT` (`off`) | `on` puts full prompts, replies and tool args into spans (Phoenix playback); off = structure + sha256 (F1) |
| `DEBUG_PANEL` (`off`) | `on` adds `policy`, `tool_calls`, `guardrails` to responses and full detail to 4xx bodies (console); off = `answer` + `trace_id` (F5/F6) |

Constants in `config.py`: `POLICY_TIMEOUT_S=2.0`, `GUARDRAIL_TIMEOUT_S=30.0`,
`EMBED_MODEL=text-embedding-3-small`, the three system prompts (`assistant-system-v1` donor support copilot,
`operations-agent-v1` Finance-role Operations Agent, `story-generator-v1`), chat
`max_tokens=600`, OpenAI client `max_retries=0, timeout=90`.

### Hard agent limits (`/run`)

Deterministic execution boundaries, independent of model behavior:

| Limit | Default | Enforced | Termination reason |
|---|---|---|---|
| `MAX_TOOL_ROUNDS` | 4 | model turns in the tool loop | `max_steps` |
| `MAX_TOOL_CALLS` | 20 | total tool invocations across all rounds (one round can request several) | `max_tool_calls` |
| `MAX_RUNTIME_S` | 120 | wall clock for the whole run, checked between rounds and between tool calls | `max_runtime` |
| `MAX_RETRIES_PER_TOOL` | 2 | same `(tool, args)` pair failing repeatedly short-circuits to `retry_limit_exceeded` without calling the backend again | — |
| `MAX_TOKENS_PER_RUN` | 20000 | cumulative `gen_ai.usage.total_tokens` across every `chat` call this run, checked before each round | `max_tokens` |
| loop detector (`limits.detect_loop`) | period 1-3, ~2.5 cycles | the tool-name sequence (e.g. `A,B,A,B,A`) | `repeated_tool_execution` |

`MAX_TOKENS_PER_RUN` is a `max_cost_per_run` proxy: LiteLLM owns the real per-key $ budget
(`gateway/litellm-config.yaml`) and that figure is not cheaply available per-request to this runtime, but token usage
already rides on every chat response and tracks cost closely enough to bound a run.

On any non-`complete` termination the run stops, a fixed message from `limits.TERMINATION_MESSAGES` is returned as the
answer (still through the output guardrail), and the panel's `run` object reports `{steps, tool_calls, tokens_used,
runtime_s, termination_reason}`. The root span carries `termination.reason`, `agent.step`, `agent.tool_calls`,
`agent.tokens_used`.

### Agent memory (`memory.py`, Phase 8)

Separated by purpose, in the `kb` database (`agent_memory` table — the runtime already holds credentials there; no
new secret or database):

| Kind | Tool | Written by | Scope |
|---|---|---|---|
| `preference` | `get_user_context()` | nothing in the tool layer — seeded only (`fixtures.MEMORY_PREFS`) | `(tenant_id, user_id)`, long-lived |
| `task_state` | `get_session_context()` / `save_task_state(key, value)` | `save_task_state` only | `(tenant_id, user_id, session_id)` |

`session_id` is an optional field on `/run`'s request body, carried on `ctx["session_id"]` and the root span as
`agent.session.id`; it is a caller-supplied continuity key, not a server-generated conversation thread. Without one,
`get_session_context`/`save_task_state` return `bad_args` rather than guessing an id. `save_task_state` validates
before writing: `memory.validate_key` (`^[a-zA-Z0-9_.-]{1,64}$`) and `memory.validate_value` (JSON-serializable,
≤ 4000 bytes encoded) — a model can never write an arbitrary blob or an unbounded one. The model cannot write
`preference` rows at all: there is no tool for it, by design (plan Phase 8: "do not allow the LLM to arbitrarily
write long-term memory").

Every memory tool still goes through `policy.decide("tool", ...)` on the caller's own tenant like any other tool —
`get_user_context`/`get_session_context` are `low` risk, `save_task_state` is `medium` (a write, even though fully
self-scoped) — see [opa.md](opa.md#risk-classification).

## Human approval workflow (`approvals.py`, Phase 10)

The one path OPA's tool stage never grants on its own: a `high`/`critical` `action_risk` tool (today, only
`resend_receipt`). Lifecycle: `pending -> rejected | executed | failed`, in the `kb` database's `approval_requests`
table (same credentials as `memory.py`, no new secret).

1. The model calls `resend_receipt(donation_id)`. `tools._resend_receipt` runs PIP → `policy.decide("tool", ...)`
   exactly like any by-id tool. OPA denies with `reason=requires_approval` (the tool is `high` risk). Instead of a
   dead end, the handler calls `approvals.create(...)` with the tool name and args **exactly as the model sent
   them** and returns `{"error": "requires_approval", "approval_id": "APR-..."}` to the model, which tells the user.
   (The id is prefixed, not a bare hex blob: a raw `uuid4().hex` reads as a leaked API key to the output
   guardrail's `self_check_output` rail -- console finding, 2026-10-04.)
2. `GET /approvals` (pending queue, caller's own tenant) / `GET /approvals/{id}` (single row; the original requester
   or a same-tenant Finance approver) / `POST /approvals/{id}/decide {decision: "approve"|"reject"}` are plain
   Bearer-JWT endpoints on this same FastAPI app, not agent tools -- a human (today: Finance, this POC's one
   business role) drives them directly, not the model. The decide endpoint takes no tool or args, only a decision
   on an id -- **approval integrity**: it can never run a different action than the one a human actually saw.
3. A reject is terminal. An approve re-asks OPA -- a separate `approval_execute` stage (role/tool/tenant/risk, fresh,
   not just "the row says pending"; see [opa.md](opa.md#risk-classification)) -- and only on that fresh allow calls
   `tools.execute_approved(tool, args, ctx)`, which runs the exact stored args with no model or tool loop involved.
   The row is then marked `executed` (with the real result) or `failed` (OPA denied, or the backend call failed).

Who may list/decide is a direct role check (`claims["role"] == "Finance"`) plus a tenant filter on every query, not
an OPA call -- these endpoints execute nothing by themselves, so the "second, independent layer" pattern
`raisin-api`'s own `/api/*` routes already use applies here too (see [opa.md](opa.md)). The OPA call that actually
gates execution is `approval_execute`, step 3 above.

## Interface

| Endpoint | Body | Returns |
|---|---|---|
| `POST /run` | `{question, model?, temperature?, session_id?}` + Bearer JWT | `{answer, trace_id}` (+ panel) or 4xx/5xx `{detail: {error, stage?, reason?, ..., trace_id}}` |
| `POST /story` | `{free_text, model?}` + Bearer JWT | `{story, social_post, trace_id}` (+ `model_saw`, panel) |
| `GET /approvals` | + Bearer JWT (Finance) | `{pending: [...]}`, caller's own tenant |
| `GET /approvals/{id}` | + Bearer JWT (requester or same-tenant Finance) | the full row, including `result` once decided |
| `POST /approvals/{id}/decide` | `{decision: "approve"\|"reject"}` + Bearer JWT (Finance) | `{approval_id, status, result?}`; 404 cross-tenant, 409 already-decided |
| `GET /health` | | flags |

Error vocabulary (`error` field): `policy_deny` (403), `no_gateway_key` (403), `guardrail_block` (400), `guardrail_unavailable`
(400), `gateway_error` (502), `missing bearer token` / `invalid token: <Type>` (401).

## The request algorithm (`/run`)

1. `verify_bearer`: JWKS from `raisin-api`, RS256, `aud=aicp`, `iss=raisin-api`; one refresh-and-retry on `InvalidSignatureError`
   (key rotated under the same kid). Claims → span attributes.
2. Pick the persona by role (Phase 13): `prompt_name = "operations-agent-v1"` for Finance, else `"assistant-system-v1"`.
   `policy.decide("model", claims, model=req.model or "", prompt=prompt_name)`. Deny → 403. Allow → `model`,
   `allowed_tools`, `withheld_tools`, `retrieval_filter`, `registry_revision`.
3. Pick the LiteLLM key for `(tenant_id, role)`.
4. `guardrails.input_stage(question)`: Presidio analyze → `Placeholders` (mapping `<TYPE_n>` → original, identical strings
   share one token, replaced from the end so offsets stay valid) → guardrails `/v1/scan/input` on the sanitized text.
   Block → 400. Unavailable → 400.
5. Messages = system prompt + sanitized question; `tools.schemas_for(allowed_tools)`.
6. Loop up to `MAX_TOOL_ROUNDS` times, subject to the [hard limits](#hard-agent-limits-run): `llm.chat(...)`. No tool
   calls → answer. Else for each tool call: parse args (bad JSON → `{}`), `tools.execute(name, args, ctx)` (unless the
   same call already hit `MAX_RETRIES_PER_TOOL` failures), append the JSON result as a `tool` message. After each round,
   `limits.detect_loop` checks the tool-name history.
7. `guardrails.output_stage(answer, placeholders, role, sanitized_question)`: Presidio (EMAIL/PHONE/CC → refusal), guardrails
   `/v1/scan/output` (→ refusal), then restore (Finance/Participant) or anonymize PERSON (Donor).
8. Envelope per `DEBUG_PANEL`.

`/story` is the same without steps 5 to 6's loop (Participant has no tools), temperature 0.7, JSON parse, per-field
restoration (`Placeholders.restore` for the story, `Placeholders.social` for the post).

## Tool layer (`tools.py`)

29 handlers behind one `execute()` that opens `execute_tool <name>` and records `tool.parameters` (hashed unless debug):

| Tool | Shape | Backend | `action_risk` |
|---|---|---|---|
| `get_transaction_analysis(transaction_id)` | by-id: PIP → OPA(resource tenant) → GET | `/api/transactions/{id}/analysis` | low |
| `get_donation(donation_id)` | by-id | `/api/donations/{id}` | low |
| `get_donor_profile(donor_id)` | by-id | `/api/donors/{id}/profile` | medium |
| `list_transactions(result?, decline_category?, min_amount?, max_amount?, limit?)` | tenant-scoped: OPA(own tenant) → GET with params | `/api/transactions` | low |
| `get_tenant_donation_summary()` | tenant-scoped | `/api/summary` | low |
| `find_donor(email)` | tenant-scoped; `_resolve` swaps `<EMAIL_ADDRESS_1>` for the real value in-process; POST body | `/api/donors/lookup` | medium |
| `get_my_donations()` | tenant-scoped; identity from the JWT, no argument | `/api/me/donations` | low |
| `search_kb(query)` | OPA(own tenant) → embed via LiteLLM → pgvector with `retrieval_filter` → `guardrails.retrieved_stage` | `kb.chunks` | low |
| `get_user_context()` | self-scoped: OPA(own tenant) → `memory.get_user_context` | `kb.agent_memory` (read-only) | low |
| `get_session_context()` | self-scoped; `bad_args` if no `session_id` on the run | `kb.agent_memory` | low |
| `save_task_state(key, value)` | self-scoped; validated (`memory.validate_key`/`validate_value`) before write; `bad_args` if no `session_id` | `kb.agent_memory` | medium |
| `resend_receipt(donation_id)` | by-id; offered to Finance, but OPA's tool stage always denies with `requires_approval` first (Phase 10) | `/api/donations/{id}/resend-receipt` | high |

Transaction-investigation tool set (implementation plan Phase 3, literal plan tool names -- see `raisin-api.md#system-of-record-tools-call-these`
for each one's endpoint and return shape). All `action_risk=low` (Phase 1 autonomy: READ+ANALYZE+RECOMMEND only):

| Tool | Shape | `action_risk` |
|---|---|---|
| `get_campaign(campaign_id)` | by-id (PIP kind `campaign`) | low |
| `get_campaign_statistics(campaign_id)` | by-id | low |
| `get_transaction(transaction_id)` | by-id | low |
| `search_transactions(result?, decline_category?, campaign_id?, gateway?, min_amount?, max_amount?, start_time?, end_time?, limit?)` | tenant-scoped; `list_transactions`'s full-filter sibling | low |
| `get_transaction_statistics(campaign_id?, start_time?, end_time?, status?)` | tenant-scoped | low |
| `compare_transaction_periods(campaign_id?, current?, previous?)` | tenant-scoped; `current`/`previous` default `last_hour`/`previous_hour` | low |
| `get_decline_statistics(campaign_id?, start_time?, end_time?)` | tenant-scoped | low |
| `get_payment_gateway_statistics(campaign_id?, start_time?, end_time?)` | tenant-scoped | low |
| `get_fraud_signals(campaign_id?, start_time?, end_time?, min_fraud_score?, limit?)` | tenant-scoped | low |
| `get_application_errors(service?, start_time?, end_time?, limit?)` | tenant-scoped | low |
| `get_incident_history(status?, start_time?, end_time?, limit?)` | tenant-scoped | low |

Controlled actions (implementation plan Phase 14): graduated autonomy per Phase 9 -- `restart_worker`/
`clear_failed_job`/`block_ip_temporarily` are `medium`, the rest `low`; OPA's tool stage auto-executes both tiers
once role/tenant checks pass, same as `find_donor`/`save_task_state` above, no `requires_approval` branching like
`resend_receipt`. No worker fleet, job queue, WAF or pager exists in this POC; each backend call is a no-op
status-flip audited to `remediation_actions`, except `create_incident`, which writes a real `incidents` row:

| Tool | Shape | Backend | `action_risk` |
|---|---|---|---|
| `restart_worker(worker)` | tenant-scoped | `/api/workers/restart` | medium |
| `clear_failed_job(job_id)` | tenant-scoped | `/api/jobs/clear` | medium |
| `block_ip_temporarily(ip, duration_minutes?)` | tenant-scoped; `ip` validated server-side, `duration_minutes` 1-1440 (default 120) | `/api/security/block-ip` | medium |
| `create_incident(title, severity, summary)` | tenant-scoped; real `incidents` row, `status="open"` | `/api/incidents` | low |
| `send_notification(channel, message)` | tenant-scoped; `channel` one of slack\|email\|pagerduty | `/api/notifications` | low |
| `collect_diagnostic_bundle(service?, start_time?, end_time?)` | tenant-scoped; summarizes `application_errors`/open `incidents` | `/api/diagnostics/collect` | low |

`block_ip_temporarily`'s evidence comes from `transactions.ip_address` (nullable; only the campaign 2 card-testing
cluster fixture sets it), surfaced through `get_fraud_signals`/`get_transaction`/`search_transactions` so the model
can name a real offending IP instead of guessing one. The operations persona (below) is told these six are
pre-approved -- call directly once the investigation supports it, unlike `resend_receipt` which always needs a
human.

The Operations Agent system prompt (`config.PROMPTS["operations-agent-v1"]`, the role/objective/rules-structured persona
Finance gets) tells the model it does not know the current date/time and to prefer `compare_transaction_periods`'s
relative `current`/`previous` periods over guessing absolute `start_time`/`end_time` for a relative question ("the last
hour") -- without that, a cheap model will sometimes invent a plausible-looking but wrong ISO timestamp (observed with
gpt-4o-mini: `2023-10-05T...`) and silently get an empty result set. The Donor role never sees these transaction tools,
so its `assistant-system-v1` persona is scoped to `get_my_donations` and `search_kb` only.

By-id tools return `{"error": "not_found"}` for a missing id, a foreign-tenant id (OPA deny) and a backend 403/404 alike.
Tenant-scoped tools return `{"error": "denied", "reason": ...}` on OPA deny and `{"error": "bad_args"}` on backend 422.
`resend_receipt` is the one exception to both: on `requires_approval` it returns `{"error": "requires_approval",
"approval_id": "APR-..."}` instead of a dead end — see [Human approval workflow](#human-approval-workflow-approvalspy-phase-10).

## Spans

`assistant.run` / `story.run`, `policy.decide`, `guardrail.input` → `guardrail.presidio.analyze` + `guardrail.scan.prompt`,
`chat <model>`, `execute_tool <name>` → `pip.<kind>_tenant` + `policy.decide` (+ `retrieve search_kb` → `embeddings`,
`db.query`; `guardrail.retrieved` → `guardrail.scan.retrieved_chunk` ×n), `guardrail.output` → `guardrail.presidio.analyze` +
`guardrail.scan.output` (+ `guardrail.presidio.anonymize`); `approval.decide` → `policy.decide` (stage `approval_execute`)
+ `execute_approved <tool>` on an approve. Full attribute list: [../05-observability-reference.md](../05-observability-reference.md#agent-runtime).

## When it is down

`/assistant` and `/story` fail at `raisin-api`'s forward (connection error → 500). `/donate` and everything else continue.
Because it is the only holder of `RUNTIME_SERVICE_TOKEN`, nothing else can reach `/api/*` while it is down.

## Success example

Scenario 7 (trace `dee6bf1bcad53ff948e62732dfb459f9`): question contains `alex.a@example.com`.

```
guardrail.input           placeholders=1   entities=[EMAIL_ADDRESS:1]     → model receives "...the donor <EMAIL_ADDRESS_1> made..."
chat gpt-4o-mini          gen_ai.tool.calls=[find_donor]                  → args {"email": "<EMAIL_ADDRESS_1>"}
execute_tool find_donor   tool.result=ok  tool.arg.resolved_placeholder=true
  policy.decide           stage=tool allow
  raisin-api POST /api/donors/lookup   (body carries the real email; never a URL attribute)
chat gpt-4o-mini          → "The donor with ID 24 has made 2 donations with a total approved amount of 40.0 CAD. ..."
guardrail.output          pass
```

The `tool_calls` entry in the response still shows the placeholder, not the address.

Phase 10, live console round trip (2026-10-04): Finance A asks `/assistant` "Please resend the tax receipt for
donation 873928." →  `execute_tool resend_receipt` → `policy.decide` deny `requires_approval` risk=high →
`approvals.create` → answer tells the user "request **APR-15edf95bef46** needs sign-off". A same-tenant Finance
approver then calls `POST /approvals/APR-15edf95bef46/decide {"decision": "approve"}` directly (Bearer JWT, no
model involved) → `policy.decide` stage=`approval_execute` allow → `execute_approved resend_receipt` →
`raisin-api POST /api/donations/873928/resend-receipt` → `{"approval_id": "APR-...", "status": "executed",
"result": {"donation_id": 873928, "receipt_resent_at": "2026-10-04T..."}}`.

Phase 14, live console round trip (2026-10-04): tenant-c Finance asks "Check campaign 2 for fraud signals. If you
find a clear card-testing pattern, block the worst offending IP and open an incident describing what you found." →
`get_fraud_signals(campaign_id=2)` → 5 flagged transactions, all `ip_address=203.0.113.50` → `policy.decide` stage=
tool allow risk=medium for `block_ip_temporarily(ip="203.0.113.50", duration_minutes=120)` → `raisin-api POST
/api/security/block-ip` (no approval request, unlike `resend_receipt`) → `create_incident(...)` risk=low, same auto-
execute path → final answer: "I found a clear card-testing pattern from IP 203.0.113.50, with 5 flagged
transactions and an average fraud score of 0.918. I have temporarily blocked this IP for 120 minutes and opened an
incident titled "Fraud Signals Detected" to document the findings." Three tool calls, zero human approvals.

## Failure examples

Fail closed on OPA (F9), trace `a1f7a8417c2caa1e4ae415aa2fc94483`:

```
HTTP 403 {"detail": {"error": "policy_deny", "stage": "model", "reason": "unavailable", "decision_id": "", "trace_id": "a1f7a841..."}}
assistant.run ERROR
 policy.decide  stage=model result=deny reason=unavailable  events [exception, policy.deny]
```

Fail closed on Presidio (F10), trace `c595b2b29baa4793a254071f6fdbffaf`:

```
HTTP 400 {"detail": {"error": "guardrail_unavailable", "service": "presidio-analyzer", "verdict": "unavailable", "trace_id": "c595b2b2..."}}
```

Tenant mismatch on a tool (F6): model called `get_donation(873928)` as Finance B; `pip.donation_tenant` → tenant-a;
`policy.decide` deny `tenant_mismatch`; `execute_tool` `tool.result=denied`; model received `not_found`; HTTP 200.

## Related

- Design and change log: `../../../02_Donation_AI_Control_Plane_POC_Design.md`
- Tool authorization diagram: `../../../diagrams/10-poc-tool-call-authorization.png`
- Neighbours: [opa.md](opa.md), [guardrails.md](guardrails.md), [litellm.md](litellm.md), [raisin-api.md](raisin-api.md)
