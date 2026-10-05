# Observability reference: spans, attributes, SIEM filter

One trace per request across every container. This page lists what each service emits and how the collector routes it.
Verified against `services/common/otel_setup.py`, `services/agent-runtime/*.py`, `services/guardrails/app.py`,
`services/raisin-api/app.py`, `observability/otel-collector.yaml` on 2026-09-07; litellm attributes and the step-by-step table against live trace `0b3bab982fb126ad83702a2fc2fe0bcd` on 2026-09-12.

![Observability pipeline](../../diagrams/12-poc-observability-pipeline.png)

Source: `../../diagrams/12-poc-observability-pipeline.mmd`.

## Propagation

- Every Python service calls `init_tracing(service_name)`: OTLP/HTTP exporter to `otel-collector:4318/v1/traces`,
  `BatchSpanProcessor`, resource attributes `service.name` and `openinference.project.name=ai-control-plane-poc`.
- `HTTPXClientInstrumentor` is on in every service, so outbound `httpx` calls (OpenAI SDK → LiteLLM, runtime → OPA /
  guardrails / raisin-api / Presidio) carry `traceparent`. Header and body capture stay off.
- `FastAPIInstrumentor` is on with `health,healthz` excluded, which produces the `POST /assistant`, `POST /run`,
  `GET /api/...` server spans and their `http receive` / `http send` children.
- LiteLLM exports through its own `otel` callback; OPA through `distributed_tracing` (gRPC to the collector, 100 % sampled);
  NeMo Guardrails through `opentelemetry-instrument` auto-instrumentation.
- `trace_id` is returned to the caller in the JSON body and in the `X-Trace-Id` response header from `raisin-api`.

## Span catalogue

### agent-runtime

| Span | Kind / marker | Key attributes | Events |
|---|---|---|---|
| `assistant.run` / `story.run` | root of the AI path | `gen_ai.prompt.name`, `enduser.id`, `tenant_id`, `role` (set by JWT verification); `assistant.run` also sets `agent.session.id` (the request's `session_id`, or `""`) up front, then `termination.reason` (`complete` \| `max_steps` \| `max_tool_calls` \| `max_runtime` \| `max_tokens` \| `repeated_tool_execution`), `agent.step`, `agent.tool_calls`, `agent.tokens_used` once the tool loop ends | `exception` on any 4xx/5xx |
| `policy.decide` | one per OPA call | `policy.engine=opa`, `policy.stage` (`model` \| `tool` \| `approval_execute`), `policy.input.model`, `policy.input.tool`, `policy.input.prompt`, `policy.input.resource_tenant`, `policy.result` (`allow` \| `deny`), `policy.reason`, `policy.decision_id`, `policy.registry_revision`; model stage adds `policy.model`, `policy.tools.allowed`, `policy.tools.withheld`; tool and `approval_execute` stages add `policy.tool.risk` (`low`\|`medium`\|`high`\|`critical`\|`unknown`) | `policy.deny {stage, reason, tool, model}` |
| `guardrail.input` | input stage parent | `guardrail.placeholders`, `guardrail.verdict` (`pass` \| `block`) | `guardrail.block {stage: input, reasons}` |
| `guardrail.presidio.analyze` | | `guardrail.service=presidio-analyzer`, `guardrail.entities` (e.g. `["EMAIL_ADDRESS:1","PERSON:1"]`), `guardrail.verdict=unavailable` on failure | `exception` |
| `guardrail.presidio.anonymize` | Donor output only | `guardrail.service=presidio-anonymizer`, `guardrail.entities_masked` | |
| `guardrail.scan.prompt` / `guardrail.scan.retrieved_chunk` / `guardrail.scan.output` | one per call to the guardrails service | `guardrail.service=guardrails`, `guardrail.verdict` (`pass` \| `block` \| `flagged` \| `unavailable`), `guardrail.risk_score`, `guardrail.reasons`, `guardrail.rules.hits`, `guardrail.nemo.status`, `guardrail.model` (classifier) | |
| `guardrail.retrieved` | retrieved stage parent | `guardrail.chunks`, `guardrail.flagged`, `guardrail.verdict` (`pass` \| `flagged`) | `guardrail.flagged {stage: retrieved, chunks: [doc ids]}` |
| `guardrail.output` | output stage parent | `guardrail.verdict` (`pass` \| `flagged` \| `block`) | `guardrail.block {stage: output, entities | reasons}` |
| `chat <model>` | `openinference.span.kind=LLM` | `gen_ai.operation.name=chat`, `gen_ai.system=litellm`, `gen_ai.request.model`, `gen_ai.request.temperature`, `gen_ai.response.model`, `gen_ai.response.finish_reasons`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.tool.calls`, `llm.model_name`, `llm.invocation_parameters`, `llm.token_count.*`, `input.value`, `output.value`, `input.mime_type` | `exception` + status ERROR on provider failure |
| `embeddings text-embedding-3-small` | `openinference.span.kind=EMBEDDING` | `gen_ai.operation.name=embeddings`, `gen_ai.request.model`, `gen_ai.usage.input_tokens` | |
| `execute_tool <name>` | `openinference.span.kind=TOOL` | `gen_ai.tool.name`, `tool.name`, `tool.parameters`, `tool.result` (`ok` \| `not_found` \| `denied` \| `bad_args` \| `unknown_tool` \| `requires_approval`), `tool.deny_reason`, `tool.result.fields`, `tool.arg.resolved_placeholder` (find_donor), `approval.id` (resend_receipt, on `requires_approval`) | |
| `approval.decide` | root of `POST /approvals/{id}/decide` | `approval.id`, `approval.tool`, `approval.decision` (`approve` \| `reject`), `policy.result` (on an approve) | |
| `execute_approved <tool>` | `openinference.span.kind=TOOL` | same shape as `execute_tool`, run from `/approvals/{id}/decide` on an approve, not from the tool loop | |
| `pip.<kind>_tenant` | Policy Information Point | `<kind>.id` (e.g. `donation.id`) | |
| `retrieve search_kb` | `openinference.span.kind=RETRIEVER` | `retrieval.filter.tenants`, `retrieval.filter.classifications`, `retrieval.documents` (`doc#chunk:score`) | |
| `db.query` (under retrieve) | | `db.system=postgresql`, `db.operation=SELECT`, `db.sql.table=chunks`, `db.rows` | |

**Content minimization (security review F1).** `input.value`, `output.value` and `tool.parameters` are produced by
`llm.content_attr`. With `DEBUG_TRACE_CONTENT=off` (default) the value is
`{"messages": 2, "roles": ["system","user"], "tool_results": 0, "chars": 1234, "sha256": "16 hex", "content": "redacted"}`
(or `{"keys": [...], ...}` for tool args, `{"kind": "text", ...}` for plain replies). With `on`, the first 8000 characters
of the real payload. `make test` asserts the redacted shape when the runtime reports the flag off.

### guardrails

| Span | Attributes |
|---|---|
| `guardrail.nemo` | `guardrail.stage` (`input` \| `retrieved` \| `output`), `guardrail.nemo.rail_types`, `guardrail.model` (judge), `guardrail.nemo.status` (`passed` \| `blocked` \| `modified`), `guardrail.nemo.rail` (blocking rail name), `guardrail.verdict` (`pass` \| `hit`) |
| `guardrail.classifier` | `guardrail.stage`, `guardrail.model`, `guardrail.risk_score` (authoritative score), `guardrail.classifier.text_score`, `guardrail.classifier.sentence_score`, `guardrail.verdict` |
| `guardrail.moderation` | `guardrail.moderation.enabled`, `guardrail.model`, `guardrail.risk_score` (max category score), `guardrail.moderation.categories`, `guardrail.verdict` |
| `guardrail.rules` (only `RULES_LAYER=on`) | `guardrail.stage`, `guardrail.rules.hits`, `guardrail.verdict` |

Under `guardrail.nemo` you will find `nemo-guardrails` FastAPI spans and, nested under those, LiteLLM's
`Received Proxy Server Request` → `litellm_request` with `gen_ai.usage.*` for the judge call (typically ~470 input tokens,
1 output token: "Yes" or "No").

### raisin-api

| Span | Attributes |
|---|---|
| `POST /auth/token`, `POST /assistant`, `POST /story`, `POST /donate`, `GET /api/...`, `POST /api/donors/lookup`, `GET /internal/.../tenant` | FastAPI server spans; `http.status_code`; `enduser.id`, `tenant_id`, `role` after JWT verification |
| `POST /donate` | `tenant_id`, `donation.id`, `payment.result` (never donor fields) |
| `/api/*` | `authz.tool`, `authz.result` (`allow` \| `deny_no_service_token` \| `deny_role` \| `deny_tenant_mismatch`); on role deny also `policy.result=deny`, `policy.reason=tool_not_in_role` so the SIEM filter catches it; `tool.result.fields` |
| `db.query` | one per SQL statement (`table` label in code) |

### payment-gateway

`POST /charge` with `payment.result`, `payment.amount`, `payment.currency`, `payment.decline_code`. Never any `gen_ai.*`.

### litellm

`Received Proxy Server Request` → `auth`, `postgres`, `proxy_pre_call`, `router`, `litellm_request`, `raw_gen_ai_request`,
`batch_write_to_db`. These nest under the runtime's `chat <model>` span (and under `nemo-guardrails` / `guardrail.moderation`
for the judge and moderation calls) because the httpx clients carry `traceparent`.

| Span | Attributes |
|---|---|
| `litellm_request` | `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens` / `output_tokens` / `total_tokens`, `gen_ai.cost.*`, `litellm.model_group`, `litellm.provider.model`, `metadata.user_api_key_alias` (which virtual key), `llm.request.type`. No message content. |
| `raw_gen_ai_request` | dropped by the collector (`filter/drop_raw_gateway_span`), so it appears in no backend |

**Gateway content, F14 (found and fixed 2026-09-12).** Until that day `litellm_request` carried `gen_ai.input.messages` /
`gen_ai.output.messages` and `gen_ai.completion.N.function_call.*`, and `raw_gen_ai_request` echoed the raw provider request
and response (`llm.openai.messages`, `llm.openrouter.choices`, `llm.None.input` = embedding input text, `llm.None.data` =
vectors), all regardless of `DEBUG_TRACE_CONTENT`. Two layers now stop it: `turn_off_message_logging: true` in
`gateway/litellm-config.yaml` (LiteLLM's `NO_CONTENT` capture mode; also redacts `LiteLLM_SpendLogs`, so
`STORE_PROMPTS_IN_SPEND_LOGS` no longer stores prompts) and the collector's `filter/drop_raw_gateway_span` +
`attributes/no_message_content` processors. `make test` check 4 (`gateway content`) asserts it on every span of every service
and is never skipped. Record: `../../03_AI_Control_Plane_Security_Review.md`, addendum 2026-09-12.

### opa

`POST /v1/data/{path...}` server spans (gRPC-exported), nested under the runtime's `policy.decide`. Decision content is in
OPA's console decision log, keyed by the same `decision_id` the runtime puts on its span.

### seed

`seed.schema`, `seed.keys` (`keys.created`), `seed.index` (`kb.docs`), `seed.registry` (`policy.registry_revision`). Each is
its own root so seed traces never mix with request traces.

## Following one request step by step: where each container's input and output live

One request is one trace; the table walks scenario 1 (`Why was donation 873928 declined?`) in span order. `make trace
TRACE_ID=<id>` (`scripts/trace_walk.py`, `ARGS=--full` for untruncated values) prints exactly this tree from the Jaeger API.
The `trace_id` is in the API response body, in the `X-Trace-Id` header, and in `make demo` output.

| # | Container | Span | Input you can read | Output you can read | Gate |
|---|---|---|---|---|---|
| 1 | raisin-api | `POST /assistant` | no body capture; `enduser.id`, `tenant_id`, `role` from the JWT | `http.status_code`; the answer only in the caller's response | none |
| 2 | agent-runtime | `assistant.run` | `gen_ai.prompt.name` (which system prompt) | `exception` event on 4xx/5xx | none |
| 3 | agent-runtime → opa | `policy.decide` → `POST /v1/data/{path...}` | `policy.input.*` scalars; the full OPA input JSON (subject, stage, tool, resource) in the decision log: `docker compose logs opa \| grep <policy.decision_id>` | `policy.result`, `policy.reason`, `policy.tools.allowed` / `withheld`, `policy.registry_revision`; full result in the same log line | none |
| 4 | agent-runtime → presidio-analyzer | `guardrail.presidio.analyze` | not recorded (Presidio emits no spans) | `guardrail.entities` as `TYPE:count`, never values; `guardrail.placeholders` on the parent | none |
| 5 | agent-runtime → guardrails | `guardrail.scan.prompt` → `POST /v1/scan/input` → `guardrail.nemo`, `guardrail.classifier` | scanned text is on no guardrails span | `guardrail.verdict`, `guardrail.risk_score`, `guardrail.nemo.status` / `.rail`, `guardrail.reasons`; `guardrail.block` event on the parent | none |
| 6 | guardrails → nemo-guardrails → litellm | `POST /v1/checks` → `litellm_request` | not in traces: the judge prompt is visible only in `docker compose logs nemo-guardrails` with `--verbose` on its CMD | `guardrail.nemo.status` on the guardrails span; the nested LiteLLM span has tokens and cost only | gateway exports no content (F14 fixed) |
| 7 | agent-runtime | `chat <model>` (kind LLM) | `input.value`: the exact message array sent (system prompt, placeholdered question, earlier tool results) | `output.value`: requested tool calls or the final text; `gen_ai.tool.calls`, `gen_ai.usage.*` | `DEBUG_TRACE_CONTENT`; off = roles, counts, sha256 |
| 8 | litellm | `Received Proxy Server Request` → `litellm_request` | which virtual key (`metadata.user_api_key_alias`), model group and provider model | `gen_ai.usage.*`, `gen_ai.cost.*`, `gen_ai.response.model`; no text | gateway exports no content (F14 fixed) |
| 9 | agent-runtime | `execute_tool <name>` (kind TOOL) | `tool.parameters` (model-supplied args) | `tool.result` (`ok` / `denied` / `not_found` / `bad_args`), `tool.deny_reason`, `tool.result.fields` (names only, never values) | args: `DEBUG_TRACE_CONTENT`; result values: never |
| 10 | agent-runtime → raisin-api | `pip.<kind>_tenant` → `GET /internal/.../tenant`; `GET /api/...` | `<kind>.id`, `authz.tool` | `authz.result`, `tool.result.fields`, `db.sql.table`, `db.rows` | none |
| 11 | agent-runtime | `retrieve search_kb` (kind RETRIEVER) + `embeddings` | `retrieval.filter.tenants` / `classifications` | `retrieval.documents` = `doc#chunk:score`, never chunk text | none |
| 12 | agent-runtime → guardrails → nemo / litellm | `guardrail.output` → `POST /v1/scan/output` → `guardrail.nemo`, `guardrail.moderation` | the draft answer is on no span; it is the runtime's `output.value` of the last `chat` span (7) | verdicts, scores, `guardrail.moderation.categories`; `guardrail.block` event with entities / reasons | none |
| 13 | otel-collector | (no spans of its own) | everything above over OTLP | Jaeger: every span and attribute. Phoenix: every span; input / output panes only for OpenInference kinds (7, 9, 11). SIEM file: deny / block / flagged spans only | drops `raw_gen_ai_request`, deletes any message-shaped attribute (F14 backstop), SIEM keep-only filter |

Three views of the same request:

- **Jaeger** `http://localhost:16686/trace/<trace_id>`: raw attributes of every hop.
- **Phoenix** project `ai-control-plane-poc`, filter `context.trace_id == '<trace_id>'`: the LLM / TOOL / RETRIEVER spans
  with prompt and completion panes (text only with `DEBUG_TRACE_CONTENT=on`), token counts, latency. LiteLLM spans show as
  kind LLM with tokens and cost only.
- **Logs** for the two non-span payloads: OPA decision input / result (`docker compose logs opa | grep <decision_id>`) and
  NeMo's rail activations (`docker compose logs nemo-guardrails`; `--verbose` on its CMD prints the judge prompts too).
  LiteLLM's spend log has model, key, tokens and cost per call, prompts redacted: `curl localhost:14000/spend/logs -H "Authorization: Bearer $LITELLM_MASTER_KEY"`.

## Collector pipelines

```yaml
receivers: otlp (grpc :4317, http :4318)
processors:
  batch
  filter/drop_raw_gateway_span: drop spans named raw_gen_ai_request (LiteLLM's raw provider echo)          # F14
  attributes/no_message_content: delete gen_ai.(input|output).messages, gen_ai.(prompt|completion).N.*,   # F14
                                 llm.(input|output)_messages, llm.<provider>.(messages|choices|input|data)
  filter/security: drop spans unless policy.result == "deny" OR guardrail.verdict in ("block", "flagged")
exporters:
  otlp_grpc/jaeger  -> jaeger:4317
  otlp_http/phoenix -> phoenix:6006
  file/siem         -> /siem/security-events.jsonl
pipelines:
  traces/all:  otlp -> drop_raw_gateway_span, no_message_content, batch -> jaeger, phoenix
  traces/siem: otlp -> filter/security, drop_raw_gateway_span, no_message_content, batch -> file/siem
```

The SIEM file is raw OTLP JSON, one export batch per line, containing only the matching spans (not their parents).
`scripts/siem_tail.py` flattens it to one row per span with the attributes `enduser.id`, `tenant_id`, `role`,
`policy.stage`, `policy.result`, `policy.reason`, `policy.tool`, `guardrail.verdict`, `guardrail.service`,
`guardrail.flagged`, `guardrail.risk_score`, plus event names. The 16-character prefix is the trace id: paste the full id
from Jaeger to correlate.

## Where to look for what

| Question | Tool | How |
|---|---|---|
| What happened in request X | Jaeger | `http://localhost:16686/trace/<trace_id>`; red spans are denies, blocks and exceptions |
| All denied tool calls today | Jaeger | service `agent-runtime`, tags `policy.result=deny policy.stage=tool` |
| Every request by one user | Jaeger | tags `enduser.id=finance.b@charity-b.test` |
| What the model actually saw | Phoenix | project `ai-control-plane-poc`, filter `span_kind == 'LLM'`; needs `DEBUG_TRACE_CONTENT=on` for text, otherwise hashes |
| One request as a text tree, attributes per hop | terminal | `make trace TRACE_ID=<id>` (`ARGS=--full` for whole prompts) |
| Token and cost per request | Phoenix or LiteLLM | `gen_ai.usage.*` on `chat` spans; `curl localhost:14000/spend/logs -H "Authorization: Bearer $LITELLM_MASTER_KEY"` |
| Security events only | SIEM file | `python3 scripts/siem_tail.py -f` |
| Which registry version a decision used | Jaeger | `policy.registry_revision` on `policy.decide`; compare with `policy/data/registry.json` |
| Full OPA input for a decision | container logs | `docker compose logs opa \| grep <decision_id>` |
| Which injection classifier is loaded | guardrails | `curl localhost:18000/healthz` (`injection_model`, `fallback_used`) |

## Assertions that run against traces (`make test`)

`scripts/test_traces.py`, against the Jaeger API, lookback 6 h:

1. **PCI scope, non-vacuous**: among traces rooted at `raisin-api`, ≥ 1 touches `payment-gateway`, ≥ 1 has a `gen_ai.*`
   attribute, 0 have both.
2. **PII canary**: `canary.donor@pii-canary.test` and `604-555-0199` appear in no span attribute or event of any service.
3. **Trace content minimized**: `input.value` / `output.value` / `tool.parameters` on agent-runtime spans carry
   `sha256` + structure only (skipped if the runtime reports `DEBUG_TRACE_CONTENT=on`).
4. **Gateway content** (F14): no span of any service carries `gen_ai.input.messages`, `gen_ai.output.messages`,
   `gen_ai.prompt.N.*`, `gen_ai.completion.N.*`, `llm.input_messages.*`, `llm.output_messages.*` or
   `llm.<provider>.(messages|choices|input|data)`. Never skipped; judged on traces started after the running `litellm`
   container did, so a config fix is testable at once.
5. `--fail-closed`: stop each control service in turn, run a scenario, expect `unavailable`, restart, wait for healthy.

## Related

- Real traces annotated: [03-success-paths.md](03-success-paths.md), [04-failure-paths.md](04-failure-paths.md)
- Containers that emit these spans: [containers/README.md](containers/README.md)
