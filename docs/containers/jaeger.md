# jaeger

Jaeger v2, all-in-one, in-memory storage. The waterfall view of every request across every container. This is where a
"why did this request do that" question gets answered, and where `make test` reads traces back through the API.

| | |
|---|---|
| Image | `jaegertracing/jaeger:2.20.0` |
| Port | 16686 → 16686 (UI + API); OTLP gRPC 4317 internal (from the collector) |
| Storage | memory: traces vanish on `make down` |

## How to use it

- Open `http://localhost:16686/trace/<trace_id>` with the id from any API response or `X-Trace-Id` header.
- Search: service `raisin-api` for user-facing requests, `agent-runtime` for the AI path, `seed` for seeding.
- Tag filters that matter: `policy.result=deny`, `policy.stage=tool`, `guardrail.verdict=block`, `guardrail.verdict=flagged`,
  `enduser.id=finance.b@charity-b.test`, `tenant_id=tenant-b`, `tool.result=denied`.
- Red spans: `otel.status_code=ERROR`, set on denies, blocks, unavailable controls and provider exceptions.
- Compare two traces (select both → Compare) to see structural differences, e.g. scenario 1 vs 3: identical until
  `execute_tool get_donation`, where one has `tool.result=ok` and two backend `db.query` spans and the other has
  `tool.result=denied` and none.

## API used by the tests

`GET /api/traces?service=<name>&lookback=6h&limit=500` and `GET /api/traces/<id>`. `scripts/test_traces.py` and
`scripts/check_gate.py` use these for the PCI-scope, PII-canary and content-minimization assertions and for the hour-one
gate (LiteLLM spans nested under the runtime span).

## When it is down

Nothing user-facing changes. The collector logs export errors; `make test` fails because it cannot read traces.

## Success example

Trace `71343ded243c0f5bda29fde82d97bf74` (scenario 1): 114 spans across `raisin-api`, `agent-runtime`, `opa`, `guardrails`,
`nemo-guardrails`, `litellm`, all green. Annotated in [../03-success-paths.md](../03-success-paths.md#s1-finance-user-asks-about-own-tenants-donation).

## Failure example

Trace `2dcdd6b01a71809811e11dea88c6540d` (scenario 3): same shape, one red `policy.decide` span with
`policy.reason=tenant_mismatch` and a `policy.deny` event under `execute_tool get_donation`. Annotated in
[../04-failure-paths.md](../04-failure-paths.md#f6-tenant-mismatch-on-a-tool-call-abac-deny).

## Related

- [otel-collector.md](otel-collector.md), [phoenix.md](phoenix.md), [../05-observability-reference.md](../05-observability-reference.md)
