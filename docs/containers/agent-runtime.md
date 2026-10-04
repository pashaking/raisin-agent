# agent-runtime

The half of the **AI Gateway** box that LiteLLM does not do: the tool loop, the Policy Enforcement Point (PEP), the
guardrail client, and the place where PII placeholders live and die. Roughly 700 lines of Python across seven modules. It
holds no database credentials for the system of record; it holds LiteLLM virtual keys, an OPA token, a guardrails token
and a service token for `raisin-api`.

| | |
|---|---|
| Build | `services/agent-runtime/Dockerfile` (FastAPI, openai SDK, httpx, psycopg, PyJWT) |
| Port | 18090 → 8090 (the console and demo go through `raisin-api`, not this port) |
| Depends on | `litellm` healthy, `raisin-api` healthy, `opa` started |
| Health | `GET /health` → `{"ok": true, "trace_content": "off", "debug_panel": "on"}` |
| Code | `app.py` (endpoints, envelope), `policy.py` (PEP), `guardrails.py` (Presidio + guardrails client, stages), `tools.py` (8 handlers, PIP, placeholder resolution, RAG), `llm.py` (chat/embed spans, content minimization), `auth.py` (JWKS verification), `config.py` |

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

Constants in `config.py`: `POLICY_TIMEOUT_S=2.0`, `GUARDRAIL_TIMEOUT_S=30.0`, `MAX_TOOL_ROUNDS=4`,
`EMBED_MODEL=text-embedding-3-small`, the two system prompts (`assistant-system-v1`, `story-generator-v1`), chat
`max_tokens=600`, OpenAI client `max_retries=0, timeout=90`.

## Interface

| Endpoint | Body | Returns |
|---|---|---|
| `POST /run` | `{question, model?, temperature?}` + Bearer JWT | `{answer, trace_id}` (+ panel) or 4xx/5xx `{detail: {error, stage?, reason?, ..., trace_id}}` |
| `POST /story` | `{free_text, model?}` + Bearer JWT | `{story, social_post, trace_id}` (+ `model_saw`, panel) |
| `GET /health` | | flags |

Error vocabulary (`error` field): `policy_deny` (403), `no_gateway_key` (403), `guardrail_block` (400), `guardrail_unavailable`
(400), `gateway_error` (502), `missing bearer token` / `invalid token: <Type>` (401).

## The request algorithm (`/run`)

1. `verify_bearer`: JWKS from `raisin-api`, RS256, `aud=aicp`, `iss=raisin-api`; one refresh-and-retry on `InvalidSignatureError`
   (key rotated under the same kid). Claims → span attributes.
2. `policy.decide("model", claims, model=req.model or "", prompt="assistant-system-v1")`. Deny → 403. Allow → `model`,
   `allowed_tools`, `withheld_tools`, `retrieval_filter`, `registry_revision`.
3. Pick the LiteLLM key for `(tenant_id, role)`.
4. `guardrails.input_stage(question)`: Presidio analyze → `Placeholders` (mapping `<TYPE_n>` → original, identical strings
   share one token, replaced from the end so offsets stay valid) → guardrails `/v1/scan/input` on the sanitized text.
   Block → 400. Unavailable → 400.
5. Messages = system prompt + sanitized question; `tools.schemas_for(allowed_tools)`.
6. Loop up to 4 times: `llm.chat(...)`. No tool calls → answer. Else for each tool call: parse args (bad JSON → `{}`),
   `tools.execute(name, args, ctx)`, append the JSON result as a `tool` message.
7. `guardrails.output_stage(answer, placeholders, role, sanitized_question)`: Presidio (EMAIL/PHONE/CC → refusal), guardrails
   `/v1/scan/output` (→ refusal), then restore (Finance/Participant) or anonymize PERSON (Donor).
8. Envelope per `DEBUG_PANEL`.

`/story` is the same without steps 5 to 6's loop (Participant has no tools), temperature 0.7, JSON parse, per-field
restoration (`Placeholders.restore` for the story, `Placeholders.social` for the post).

## Tool layer (`tools.py`)

Eight handlers behind one `execute()` that opens `execute_tool <name>` and records `tool.parameters` (hashed unless debug):

| Tool | Shape | Backend |
|---|---|---|
| `get_transaction_analysis(transaction_id)` | by-id: PIP → OPA(resource tenant) → GET | `/api/transactions/{id}/analysis` |
| `get_donation(donation_id)` | by-id | `/api/donations/{id}` |
| `get_donor_profile(donor_id)` | by-id | `/api/donors/{id}/profile` |
| `list_transactions(result?, decline_category?, min_amount?, max_amount?, limit?)` | tenant-scoped: OPA(own tenant) → GET with params | `/api/transactions` |
| `get_tenant_donation_summary()` | tenant-scoped | `/api/summary` |
| `find_donor(email)` | tenant-scoped; `_resolve` swaps `<EMAIL_ADDRESS_1>` for the real value in-process; POST body | `/api/donors/lookup` |
| `get_my_donations()` | tenant-scoped; identity from the JWT, no argument | `/api/me/donations` |
| `search_kb(query)` | OPA(own tenant) → embed via LiteLLM → pgvector with `retrieval_filter` → `guardrails.retrieved_stage` | `kb.chunks` |
| `resend_receipt(donation_id)` | schema exists, registered and approved, in no role: never offered | |

By-id tools return `{"error": "not_found"}` for a missing id, a foreign-tenant id (OPA deny) and a backend 403/404 alike.
Tenant-scoped tools return `{"error": "denied", "reason": ...}` on OPA deny and `{"error": "bad_args"}` on backend 422.

## Spans

`assistant.run` / `story.run`, `policy.decide`, `guardrail.input` → `guardrail.presidio.analyze` + `guardrail.scan.prompt`,
`chat <model>`, `execute_tool <name>` → `pip.<kind>_tenant` + `policy.decide` (+ `retrieve search_kb` → `embeddings`,
`db.query`; `guardrail.retrieved` → `guardrail.scan.retrieved_chunk` ×n), `guardrail.output` → `guardrail.presidio.analyze` +
`guardrail.scan.output` (+ `guardrail.presidio.anonymize`). Full attribute list: [../05-observability-reference.md](../05-observability-reference.md#agent-runtime).

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
