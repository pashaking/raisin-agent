# litellm

The LiteLLM proxy: the routing, key, budget and telemetry half of the **AI Gateway** box. Every model call in the POC goes
through it: the assistant's chat, the story generator, the embeddings for RAG, the NeMo judge, and the moderation call.
That is what makes "keyed, budgeted and traced like any model call" true for the guardrails too.

| | |
|---|---|
| Image | `ghcr.io/berriai/litellm:v1.100.0` |
| Port | 14000 → 4000 |
| Depends on | `postgres` healthy, `otel-collector` |
| Health | `GET /health/liveliness` (start period 90 s) |
| Config | `gateway/litellm-config.yaml` (read-only mount), `STORE_MODEL_IN_DB=False` |
| Store | `postgresql://poc:...@postgres:5432/litellm` (virtual keys, spend) |

## Model list

| `model_name` (what callers ask for) | Routed to | Used by |
|---|---|---|
| `gpt-4o-mini` | `openai/gpt-4o-mini` | assistant, story, NeMo judge |
| `claude-sonnet` | `openrouter/anthropic/claude-sonnet-4.5` via `https://openrouter.ai/api/v1` | assistant, story (default for Finance/Participant in `roles.json`) |
| `omni-moderation-latest` | `openai/omni-moderation-latest` | guardrails output moderation |
| `text-embedding-3-small` | `openai/text-embedding-3-small` | RAG (runtime) and indexing (seed) |

Commented swaps in the same file: `anthropic/claude-sonnet-4-5` (direct) and
`bedrock/anthropic.claude-sonnet-4-5-20250929-v1:0` (ca-central-1). Uncomment one block and restart: the design premise
that provider change is a one-line edit outside application code.

`litellm_settings.callbacks: ["otel"]` with `OTEL_EXPORTER=otlp_http`, `OTEL_ENDPOINT=http://otel-collector:4318/v1/traces`.
`drop_params: true` so provider-specific params do not break the swap. `turn_off_message_logging: true` (F14, 2026-09-12):
the otel callback and the spend log carry no prompts or completions, only model, tokens, cost and key alias; the runtime's
gated `chat` span is the one content channel.

## Virtual keys

Created by `seed keys` with the master key. Values come from `.env`; aliases, budgets and model access from `fixtures.py`:

| Alias | Budget | Models | Held by |
|---|---|---|---|
| `tenant-a-finance` | 10 USD / day | gpt-4o-mini, claude-sonnet, text-embedding-3-small | agent-runtime for (tenant-a, Finance) |
| `tenant-b-finance` | 10 USD / day | same | agent-runtime for (tenant-b, Finance) and (tenant-b, Donor) |
| `story-generator` | 10 USD / day | same | agent-runtime for (tenant-a, Participant) |
| `seed-indexer` | 5 USD / day | same | seed (embeddings) |
| `guardrails` | 5 USD / day | `omni-moderation-latest`, `gpt-4o-mini` only | guardrails service (moderation) and nemo-guardrails (`OPENAI_API_KEY`, judge) |

Budget exhaustion or a model not in the key's list is a LiteLLM 4xx, which the runtime reports as 502 `gateway_error`.

## Spans

`Received Proxy Server Request` → `auth`, `postgres`, `proxy_pre_call`, `router`, `litellm_request` (`gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`), `raw_gen_ai_request`, `self`, `batch_write_to_db`. They nest under the caller's span (`chat <model>`,
`guardrail.nemo`, `guardrail.moderation`, `embeddings ...`) because every caller's httpx client propagates `traceparent`.
The hour-one risk gate (`make gate`) exists to prove exactly this nesting.

## When it is down

Chat and embeddings raise in the runtime → 502 `gateway_error`. Before that, the NeMo judge cannot reach its model, so the
guardrails service returns an error and the runtime answers 400 `guardrail_unavailable` at the input stage. Either way no
request passes.

## Success example

From trace `71343ded243c0f5bda29fde82d97bf74`, three separate proxy requests inside one user request:

```
guardrail.nemo (guardrails)  → litellm  litellm_request  gen_ai.usage 469 in / 1 out    key=guardrails       (judge "No")
chat gpt-4o-mini (runtime)   → litellm  litellm_request  gen_ai.usage 594 in / 18 out   key=tenant-a-finance (tool call)
guardrail.moderation         → litellm  POST /v1/moderations                            key=guardrails
```

`curl localhost:14000/spend/logs -H "Authorization: Bearer $LITELLM_MASTER_KEY"` lists each with cost and the key alias.

## Failure example

Observed 2026-09-06 before the OpenRouter account was funded: `claude-sonnet` → OpenRouter 402 → LiteLLM error → runtime
`502 {"error": "gateway_error", "message": "...", "trace_id": ...}`; the `chat claude-sonnet` span has status ERROR and the
nested LiteLLM span shows the upstream code. Workaround used at the time: `DEMO_MODEL=gpt-4o-mini make demo` (policy still
validates the override against the role's model list). Validated end to end on `claude-sonnet` once funded.

## Related

- [agent-runtime.md](agent-runtime.md), [guardrails.md](guardrails.md), [nemo-guardrails.md](nemo-guardrails.md), [seed.md](seed.md)
- Direct provider check that bypasses policy (diagnostics only): [../../RUNBOOK.md](../../RUNBOOK.md) section 7
