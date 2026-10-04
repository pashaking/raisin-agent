# ai-control-plane-poc

Runnable proof of concept of the donation-platform AI control plane described in
`../02_Donation_AI_Control_Plane_POC_Design.md` (design) and drawn in
`../diagrams/01-ai-control-plane-placement.mmd` (topology). Real gateway (LiteLLM), real policy
engine (OPA), real guardrails (NVIDIA NeMo Guardrails self-check rails + prompt-injection classifier + OpenAI moderation, Presidio for PII), real providers (OpenAI, Anthropic via OpenRouter),
real RAG (pgvector), one trace per request in Jaeger and Phoenix.

## Status (2026-09-06)

| Step | State |
|---|---|
| 1. Hour-one risk gate (trace nesting through LiteLLM, Phoenix token rendering) | PASS |
| 2. raisin-api, payment-gateway, seed | done |
| 3. OPA policy, registry as OPA data, `opa test` 15/15 | done |
| 4. Guardrails: `guardrails` orchestrator → `nemo-guardrails` (self-check input/output rails) + injection classifier + output moderation; Presidio analyzer/anonymizer; placeholders; fail-closed | done (LLM Guard replaced 2026-09-06, NeMo added same day) |
| 5. RAG: pgvector, tenant/classification filter, poisoned doc | done |
| 6. `make demo` scenarios 0–9, `make test` (PCI scope, PII canary, fail-closed, data-tool API) | done, all PASS |
| 7. Provider validation: full demo on `gpt-4o-mini` and on `claude-sonnet` (OpenRouter) | done 2026-09-06, both PASS |
| 8. Chat console `/ui` on raisin-api (user picker, assistant/story, model override, control-plane panel per reply) | done 2026-09-06 |
| 9. Data tools: 6 read-only donation / transaction / donor tools behind OPA + raisin-api, placeholder-resolved arguments | done 2026-09-07 |
| 10. Security remediation (CSO re-audit 2026-09-07): F12 role gate + service token on `/api/*`, F1 trace content minimized, F3 OPA token auth + no host port, F2 `/donate` integrity, F5/F6 response envelope behind `DEBUG_PANEL`; `opa test` 22/22, `make test-tools` all PASS | done 2026-09-07 |
| 11. Phoenix 20.11.0 (working dir moved off the image's venv path), `make trace` step-by-step trace dump, F14 gateway span content closed test-first (LiteLLM `turn_off_message_logging` + collector drop/delete processors, `make test` check 4) | done 2026-09-12 |

Known limitations (honest, on purpose):

- **Guardrails stack (LLM Guard, an archived project, removed).** `services/guardrails` orchestrates three layers and
  emits a span per layer: (1) **NVIDIA NeMo Guardrails** (`nemo-guardrails` container, `/v1/checks`): `self check
  input` on user prompts and on every retrieved chunk, `self check output` on drafts, plus YARA `injection detection`;
  the judge is `gpt-4o-mini` reached through LiteLLM with the `guardrails` virtual key, so every judge call is keyed,
  budgeted and traced (`nemo-guardrails → litellm` spans nest under `guardrail.nemo`). Prompts and rails live in
  `services/nemo-guardrails/config/aicp/` (bind-mounted; edit, then `docker compose restart nemo-guardrails`).
  (2) A prompt-injection classifier, default `meta-llama/Llama-Prompt-Guard-2-86M` (gated: accept the licence, set
  `HF_TOKEN`; otherwise falls back to the open `protectai/deberta-v3-base-prompt-injection-v2` and says so on
  `/healthz` and on every span). (3) OpenAI `omni-moderation-latest` on output, also via LiteLLM. The regex filter
  layer (`rules.py`) is off by default; `RULES_LAYER=on` adds it back as deterministic defense in depth. Every hit is
  named (`nemo:self check input`, `classifier:INJECTION`, `moderation:violence`, `rule:tool_coercion`).
- **Cost and latency of LLM-judged rails.** Each NeMo check is one small model call (~0.45 s, ~20 output tokens);
  an assistant request adds one input check, one per retrieved chunk (4), and one output check. Scenario 1 runs in
  ~2.8 s end to end on `gpt-4o-mini`. Judge verdicts are not deterministic; the classifier and moderation layers are
  the deterministic backstop.
- **Classifier false positives on procedural docs.** NeMo passes all 9 benign knowledge docs and blocks the poisoned
  one; the fallback classifier still flags 2 of 9 benign docs (`decline-codes-runbook.md`, `donor-faq.md`) on whole-text
  score. Retrieval verdicts remain `flagged`, never a drop; policy on tool calls and the output scan are the controls
  that hold. Follow-on: measure Prompt Guard 2 once `HF_TOKEN` is in place.
- **Scenario 4 model behaviour is non-deterministic.** `gpt-4o-mini` at temperature 0 did not follow the injected
  instruction in any run so far. The deterministic evidence is the flagged retrieval span; when the model does bite,
  the trace also shows the tool-stage deny and the output block.
- **OpenRouter.** The Finance and Participant default model is `claude-sonnet`, routed as
  `openrouter/anthropic/claude-sonnet-4.5` with `OPENROUTER_API_KEY`. If the OpenRouter account has no credit the
  provider returns 402 and the runtime answers 502 `gateway_error` (visible in the trace). Run the demo with
  `DEMO_MODEL=gpt-4o-mini` in the meantime; policy still validates the override against the role's model list.
  Validated end to end on `claude-sonnet` 2026-09-06 once the account was funded.
- **Presidio and synthetic data.** Presidio ignores emails on non-existent TLDs (`.test`) and scores phone numbers
  0.4 without context words, so the scannable fixtures use `example.com` and the runtime applies per-entity
  thresholds (PERSON/EMAIL/CC 0.6, PHONE 0.4). Identity emails stay on `.test`; they are never scanned.

## Security posture after the 2026-09-07 re-audit

Review + findings: `../03_AI_Control_Plane_Security_Review.md` (re-audit section) and `.gstack/security-reports/`.
Closed in this round (each with a test in `scripts/test_tools.py` or `scripts/test_traces.py`):

- **F12 (HIGH, new)** `raisin-api /api/*` now requires the runtime's `X-Service-Token` AND checks the JWT role against the
  same `policy/data/roles.json` OPA uses (endpoint → backing tool → role). Donor/Participant tokens get 403 on finance
  routes; end users cannot reach tool-backing routes at all. OPA remains the decision point for the LLM path; the API
  re-checks it (defense in depth, two layers that must both be wrong).
- **F1 (HIGH)** LLM spans carry structure + sha256, never message bodies or tool args, unless `DEBUG_TRACE_CONTENT=on`.
  `make test` asserts it (`trace content` check). Phoenix prompt playback needs the flag.
- **F14 (HIGH, found 2026-09-12)** The gateway's own spans exported every prompt, completion, raw provider response and
  embedding input regardless of that flag. Closed the same day: LiteLLM `turn_off_message_logging: true` (also redacts the
  spend log) plus collector processors that drop `raw_gen_ai_request` and delete any message-shaped attribute. `make test`
  asserts it on every span of every service (`gateway content` check, never skipped).
- **F3 (HIGH)** OPA runs `--authentication=token --authorization=basic` with `policy/system_authz.rego`: the runtime may only
  POST decisions, seed may PUT the registry and read data, nobody uploads policy. Tokens come from `.env` via a compose
  inline config; host port 18181 is gone (`make opa-query`, `make opa-data`).
- **F2 (HIGH)** `/donate` assigns the donation id from a sequence, rejects client ids (`extra=forbid`), 404s unknown tenants,
  and never overwrites an existing donor's contact fields.
- **F5/F6 (MEDIUM)** `DEBUG_PANEL=off` (default) returns `answer` + `trace_id` only and 4xx bodies without scores or rail
  names; every by-id tool (including `get_transaction_analysis`) returns a uniform `not_found`, so a foreign id and a missing
  id are indistinguishable in the response and in the answer. `DEBUG_PANEL=on` (set in `.env` for the console demo) restores
  the control-plane panel.

Still open: F4 (shared `poc` DB role, no RLS), F7 (guardrails auth fail-open when token empty; host ports), F8 (classifier
truncation), F9 (prompts approved globally), F10 (identity stand-in, POC-known), F11 (`.env` values equal to
`.env.example`), F13 (no lockfile / hash pinning; `presidio:latest`).

## Data tools (donations, transactions, donors)

The assistant reaches the system of record only through narrow tools. Every call: OPA tool-stage decision
(`policy.decide`, ABAC on the resource's owning tenant, obtained from a service-token PIP) → `raisin-api` with the
caller's JWT (re-filters by tenant in SQL) → minimized JSON. The tenant is never a tool argument.

| Tool | Role | Returns | Notes |
|---|---|---|---|
| `get_transaction_analysis(transaction_id)` | Finance | amount, result, decline category, fraud score | original tool; since 2026-09-07 uniform `not_found` like the others |
| `get_donation(donation_id)` | Finance | amount, currency, status, donor id, its transactions | missing and foreign ids both `not_found`; the OPA deny is in the trace and SIEM feed, and in the response only with `DEBUG_PANEL=on` |
| `list_transactions(result?, decline_category?, min_amount?, max_amount?, limit?)` | Finance | rows, newest first, ≤ 50 | tenant from JWT only |
| `get_tenant_donation_summary()` | Finance | counts, totals, declines by category | |
| `get_donor_profile(donor_id)` | Finance | donor id, donation count, total approved, last status | no name, email or phone in any form |
| `find_donor(email)` | Finance | same as profile | the model passes `<EMAIL_ADDRESS_1>`; the runtime resolves it in-process; `raisin-api` takes the email in a POST body so it never lands in an `http.url` span attribute |
| `get_my_donations()` | Donor | the caller's own donations | resource owner = JWT subject |
| `resend_receipt` | none | | registered and approved, in no role: always withheld |

Design points worth showing:

- **Placeholder-resolved arguments.** Presidio replaces the email in the question before the model sees it; the model
  calls `find_donor(email="<EMAIL_ADDRESS_1>")`; `tools._resolve` maps it back only inside the runtime. The tool
  argument in the trace and in the response is the placeholder (`tool.parameters`, `tool_calls[].args`).
- **No masked email either.** A first cut returned `a***@example.com`; Presidio's output scan blocked it, and after
  reshaping to `a*** at example.com` the NeMo output rail blocked it. Allow-listing masked shapes would give the model an
  exfiltration format ("name at domain"), so the tool returns the donor id and giving summary only.
- **Rails follow scope.** Widening the assistant from single-transaction questions to lists, summaries and donor lookups
  made the `self check input` judge block ordinary questions ("Give me a summary of our donations", "Show me donation
  873928 and its transactions"). The rail prompt now distinguishes viewing from changing records and carries allowed
  examples; `scripts/test_tools.py` keeps a rail regression list (9 allowed, 5 blocked phrases). Add a phrase there
  whenever a legitimate question is blocked in the console.
- **Model tool choice moved.** With `get_donation` available, `gpt-4o-mini` answers "Why was donation 873928 declined?"
  through `get_donation` instead of `get_transaction_analysis`; scenarios 3 and 6 were re-pointed accordingly.

## Documentation

`docs/README.md` is the index: a three-command tutorial, architecture explanation, hop-by-hop request lifecycle, worked
success and failure paths with real responses and Jaeger span trees (2026-09-07), an observability reference, and one page per
container (`docs/containers/`). Diagrams 07 to 13 in `../diagrams/` describe what the POC actually runs.

## Run it

Chat console: http://localhost:18080/ui (pick a user, type a prompt; every reply shows policy decisions, tool calls, guardrail verdicts and the Jaeger/Phoenix trace links).

Step-by-step testing and monitoring guide (ad-hoc requests, OPA queries, Jaeger/Phoenix/SIEM/log views): `RUNBOOK.md`.

```bash
cp .env.example .env            # fill OPENAI_API_KEY and OPENROUTER_API_KEY
make up                          # build + start 13 services; guardrails downloads its classifier (~0.7 GB) on first start
make seed                        # schema, LiteLLM virtual keys, registry -> OPA, embed 10 docs via the gateway
make demo                        # scenarios 0-9, prints Jaeger/Phoenix links per scenario
make test                        # opa test + PCI-scope + PII-canary + gateway-content trace assertions + data-tool API assertions
make test-tools                  # data tools end to end incl. NeMo rail regression, response envelope, runtime tool loop (gpt-4o-mini; tool-loop checks need DEBUG_PANEL=on)
make test-fail-closed            # + stop/start opa, guardrails, nemo-guardrails, presidio-analyzer, presidio-anonymizer
DEMO_MODEL=gpt-4o-mini make demo # force the model while OpenRouter is unavailable
make demo S="1 3"                # subset
```

UIs: Jaeger http://localhost:16686 · Phoenix http://localhost:16006 · LiteLLM http://localhost:14000 ·
raisin-api http://localhost:18080/docs · OPA is internal only (`make opa-query`, `make opa-data`)

Demo switches in `.env` (both default off in code and compose): `DEBUG_PANEL=on` shows policy decisions, tool calls and
guardrail verdicts in every reply (the console needs it); `DEBUG_TRACE_CONTENT=on` puts full prompts and tool results into
LLM spans for Phoenix playback. Keep both off anywhere but a laptop.

## Scenarios

| # | Who asks what | What the trace shows |
|---|---|---|
| 0 | Public `POST /donate` with the canary donor's email and phone (server assigns the donation id; client ids are rejected) | `raisin-api → payment-gateway`, declined 51, zero `gen_ai.*` spans, no canary string anywhere |
| 1 | Finance A: why was 873928 declined | model ALLOW → `guardrail.input` → `chat` → `execute_tool get_donation` → PIP → `policy.decide` ALLOW → `/api/donations/873928` (with its transaction analysis) → `chat` → `guardrail.output` |
| 2 | Donor B: same question | model ALLOW with `policy.tools.withheld=[get_transaction_analysis, get_donation, list_transactions, get_tenant_donation_summary, get_donor_profile, find_donor, resend_receipt]`; only `search_kb` and `get_my_donations` offered; Donor output passes through the anonymizer |
| 3 | Finance B: same question (tenant A's donation) | tool offered, model calls `get_donation`, `policy.decide` DENY `tenant_mismatch` (red span, SIEM event); the model gets `not_found`; with `DEBUG_PANEL=off` the response is indistinguishable from a missing id |
| 4 | Finance A: our finance procedure after a decline (`gpt-4o-mini`, temp 0) | poisoned doc ranks first; `guardrail.retrieved` `flagged`; if the model bites: tool DENY on 991204 + output block on the decoy email |
| 5 | Participant A: story notes with name, phone, email | LLM span `input.value` shows `<PERSON_1>`, `<PHONE_NUMBER_1>`, `<EMAIL_ADDRESS_1>`; story restored; social post is public, so PERSON becomes first name only and any sentence carrying a phone/email placeholder is dropped whole (no "Contact me at or ." stubs) |
| 6 | Registry flip `approved=false` on `get_donation`, push, re-run 1, reset | tool disappears from `allowed_tools` without restart; `policy.registry_revision` changes; the model falls back to `get_transaction_analysis` |
| 7 | Finance A: how many donations has the donor alex.a@example.com made | `guardrail.input` placeholders=1; `execute_tool find_donor` with `tool.parameters={"email":"<EMAIL_ADDRESS_1>"}`, `tool.arg.resolved_placeholder=true`; `POST /api/donors/lookup`; answer names the donor id only |
| 8 | Finance B: list declined transactions, then a summary | `list_transactions` and `get_tenant_donation_summary`, both authorized against tenant-b; `/api/transactions` and `/api/summary` filter by the JWT tenant; no tenant-a id in the answer |
| 9 | Donor B: what donations have I made | `get_my_donations` ALLOW (resource tenant = own); `/api/me/donations` joins donors on the JWT subject |

## Ports (host → container)

Host ports are offset because another local stack already binds 5432 / 8080 / 8181.

| Service | Host | Service | Host |
|---|---|---|---|
| postgres | 15432 | raisin-api | 18080 |
| otel-collector OTLP | 14317 / 14318 | agent-runtime | 18090 |
| jaeger UI | 16686 | payment-gateway | 18070 |
| phoenix UI + OTLP | 16006 | opa | internal only (token auth) |
| litellm | 14000 | guardrails | 18000 |
| nemo-guardrails | 18010 | | |
| presidio-analyzer | 15001 | presidio-anonymizer | 15002 |

## Pinned images (2026-09-06; phoenix bumped 2026-09-12)

pgvector `0.8.6-pg16-trixie` · otel-collector-contrib `0.160.0` · jaeger `2.20.0` · phoenix `version-20.16.0` ·
litellm `v1.100.0` · opa `1.20.2` · nemoguardrails `0.24.0` (own image) · guardrails (own image: torch CPU + transformers) · presidio analyzer/anonymizer `latest` (mcr; pin when
a numbered tag appears in the manifest).

## Requirements

Docker Desktop with ≥ 12 GB memory recommended (ran on 8.3 GB with `deploy.resources.limits` on guardrails 3 GB,
Presidio 1.5 GB / 512 MB). First guardrails start downloads the classifier into the `guardrails-cache` volume.

## Layout

```
docker-compose.yml           12 services + seed + gate profiles
gateway/litellm-config.yaml  models (OpenAI, Claude via OpenRouter, commented Anthropic-direct + Bedrock swaps), otel callback
policy/                      authz.rego (+tests), system_authz.rego (OPA API authz, +tests), opa-config.yaml, data/roles.json, data/registry.json (generated)
services/guardrails/        app.py (scan API, layer spans: nemo, classifier, moderation), rules.py (opt-in regex filters), Dockerfile
services/nemo-guardrails/   Dockerfile + config/aicp/{config.yml,prompts.yml} (NeMo rails config, mounted read-only)
observability/               otel-collector.yaml (fan-out + SIEM filter), siem/security-events.jsonl
postgres/initdb/             creates litellm, raisin, kb databases (+ pgvector)
services/common/             otel_setup.py, base requirements
services/raisin-api/         identity route + JWKS, /donate, /assistant, /story, system-of-record API (donations, transactions, donors, summary, me), internal PIPs
services/agent-runtime/      tool loop, policy PEP, guardrail clients, tools (8 handlers, placeholder resolution), RAG, gate.py
services/payment-gateway/    stub
services/seed/               schema, fixtures, docs/, seed.py (all | registry | flip | reset | index)
scripts/                     demo.py, test_traces.py, test_tools.py, check_gate.py, siem_tail.py
docs/                        documentation (index, tutorial, architecture, lifecycle, success/failure paths, observability, containers/)
```

## Swaps documented in the design

- Anthropic direct or Bedrock instead of OpenRouter: uncomment the `anthropic/...` or `bedrock/...` entry in `gateway/litellm-config.yaml`.
- Entra ID instead of the built-in `/auth/token` route: Dex as a local OIDC provider; the runtime already verifies via JWKS.
