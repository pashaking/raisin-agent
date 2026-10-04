# Containers

Thirteen long-running services plus two one-shot jobs, all in `docker-compose.yml` (project name `aicp`). Host ports are
offset (14xxx / 15xxx / 16xxx / 18xxx) because another local stack binds 5432 / 8080 / 8181. Each page below covers:
purpose and design-box mapping, image or build, ports, configuration, interface (endpoints), the spans it emits, what
happens to a request when it is down, and one real success and one real failure example.

![Container topology](../../../diagrams/07-poc-container-topology.png)

Animated version with a short description per box: open `../../../diagrams/15-poc-container-flow.html` in a browser
(request, payment, span and seed flows animate in their travel direction).

| Container | Design box | Host port | Page |
|---|---|---|---|
| `raisin-api` | Identity, Application APIs, Backend + system of record | 18080 | [raisin-api.md](raisin-api.md) |
| `payment-gateway` | Payment gateway (never AI) | 18070 | [payment-gateway.md](payment-gateway.md) |
| `postgres` | RDS (raisin, kb, litellm databases) | 15432 | [postgres.md](postgres.md) |
| `agent-runtime` | AI Gateway (tool loop, Policy Enforcement Point) | 18090 | [agent-runtime.md](agent-runtime.md) |
| `litellm` | AI Gateway (routing, virtual keys, budgets) | 14000 | [litellm.md](litellm.md) |
| `opa` | Policy Engine (Policy Decision Point) | none (internal, token auth) | [opa.md](opa.md) |
| `guardrails` | Guardrails Runtime (orchestrator) | 18000 | [guardrails.md](guardrails.md) |
| `nemo-guardrails` | Guardrails Runtime (LLM-judged rails) | 18010 | [nemo-guardrails.md](nemo-guardrails.md) |
| `presidio-analyzer` | Guardrails Runtime (PII detection) | 15001 | [presidio-analyzer.md](presidio-analyzer.md) |
| `presidio-anonymizer` | Guardrails Runtime (PII masking) | 15002 | [presidio-anonymizer.md](presidio-anonymizer.md) |
| `otel-collector` | AI Observability (fan-out, SIEM filter) | 14317 / 14318 | [otel-collector.md](otel-collector.md) |
| `jaeger` | AI Observability (trace UI) | 16686 | [jaeger.md](jaeger.md) |
| `phoenix` | AI Observability (LLM span UI) | 16006 | [phoenix.md](phoenix.md) |
| `seed`, `gate` | one-shot jobs (profiles `seed`, `gate`) | none | [seed.md](seed.md) |

Startup order (compose `depends_on`): `postgres`, `jaeger`, `phoenix` → `otel-collector` → `litellm` (waits for postgres
healthy) → `nemo-guardrails`, `raisin-api`, `payment-gateway`, `opa` → `guardrails` (waits for litellm + nemo healthy) →
`agent-runtime` (waits for litellm + raisin-api healthy, opa started). `make up` runs `docker compose up -d --wait`.

Pinned images (2026-09-06): pgvector `0.8.6-pg16-trixie`, otel-collector-contrib `0.160.0`, jaeger `2.20.0`, phoenix
`version-20.16.0`, litellm `v1.100.0`, opa `1.20.2`, nemoguardrails `0.24.0` (own image), presidio `latest` (resolved to
`2.2.362` on this host; open finding F13 says pin it).

Fail-closed matrix (which request paths a container's outage stops):

| Container down | `/donate` | `/assistant` | `/story` | `/api/*` direct |
|---|---|---|---|---|
| `postgres` | 500 | 500 (raisin-api cannot mint tokens; runtime tool calls fail) | 500 | 500 |
| `raisin-api` | unreachable | unreachable | unreachable | unreachable |
| `payment-gateway` | 500 | unaffected | unaffected | unaffected |
| `agent-runtime` | unaffected | 5xx from raisin-api forward | 5xx | unaffected (403 without service token anyway) |
| `litellm` | unaffected | 502 `gateway_error` (or 400 `guardrail_unavailable` first, since NeMo's judge goes through LiteLLM) | same | unaffected |
| `opa` | unaffected | 403 `policy_deny` reason `unavailable` | same | unaffected (raisin-api uses roles.json, not OPA) |
| `guardrails` / `nemo-guardrails` | unaffected | 400 `guardrail_unavailable` service `guardrails` | same | unaffected |
| `presidio-analyzer` | unaffected | 400 `guardrail_unavailable` service `presidio-analyzer` | same | unaffected |
| `presidio-anonymizer` | unaffected | Donor requests only: 400 `guardrail_unavailable` | unaffected | unaffected |
| `otel-collector` / `jaeger` / `phoenix` | unaffected | unaffected (spans dropped by the SDK after retries; requests still served) | same | unaffected |
