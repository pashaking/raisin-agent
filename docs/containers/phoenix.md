# phoenix

Arize Phoenix: the LLM-centric view of the same traces. Where Jaeger shows every hop, Phoenix shows the spans that are
model calls (`openinference.span.kind = LLM | EMBEDDING | RETRIEVER | TOOL`) with prompts, completions, token counts,
latency and model names side by side. This is where you prove the model never saw raw PII.

| | |
|---|---|
| Image | `arizephoenix/phoenix:version-20.16.0` |
| Port | 16006 → 6006 (UI + OTLP HTTP ingest from the collector) |
| Volume | `phoenixdata` mounted at `/mnt/data` (`PHOENIX_WORKING_DIR=/mnt/data`), survives restarts and image upgrades |
| Project | `ai-control-plane-poc` (from the resource attribute every service sets) |

## What the runtime puts on LLM spans for Phoenix

From `services/agent-runtime/llm.py`: `openinference.span.kind=LLM`, `llm.model_name`, `llm.invocation_parameters`
(temperature + tool names offered), `llm.token_count.prompt / completion / total`, `input.value` + `input.mime_type=application/json`,
`output.value`. Embedding spans are kind `EMBEDDING`; `retrieve search_kb` is `RETRIEVER` with `retrieval.documents`;
`execute_tool` is `TOOL`.

**Content flag.** With `DEBUG_TRACE_CONTENT=off` (default, security review F1) `input.value` is the redacted structure
(`{"messages": 2, "roles": [...], "sha256": "...", "content": "redacted"}`), so Phoenix shows shapes and hashes, not text.
Set `DEBUG_TRACE_CONTENT=on` in `.env` and `docker compose up -d agent-runtime` for prompt playback on a laptop demo. Then
scenario 5's LLM span shows `My name is <PERSON_1> ... call me at <PHONE_NUMBER_1> or email <EMAIL_ADDRESS_1>`.

**LiteLLM child spans carry no content (F14, fixed 2026-09-12).** Nested under every `chat` span, LiteLLM's `litellm_request`
shows tokens, cost, model and key alias only; the raw provider echo span (`raw_gen_ai_request`) is dropped at the collector.
Until that day those spans carried the full messages regardless of `DEBUG_TRACE_CONTENT`; `make test` check 4 now asserts
they never do again. The runtime's `chat` span is the one prompt channel Phoenix renders.

## How to use it

- Project `ai-control-plane-poc` → Traces. Filter box: `span_kind == 'LLM'`, `attributes.llm.model_name == 'claude-sonnet'`,
  or one request: `context.trace_id == '<trace_id from the API response>'`.
- Click a span: input / output panes, token counts, latency. LiteLLM's nested spans appear as children.
- Useful for cost: sum of `llm.token_count.total` per trace; a scenario-1 request is about 1,300 chat tokens plus about
  800 judge tokens.

## Upgrading

Bump `PHOENIX_TAG` in `.env` (default in `docker-compose.yml`), `docker compose pull phoenix && docker compose up -d phoenix`,
then check `curl localhost:16006/arize_phoenix_version`, not the tag in `docker compose ps`. Phoenix runs its alembic
migrations on start (20.8.0 → 20.11.0 took 25 ms); back the volume up first if a downgrade must stay possible:
`docker run --rm -v aicp_phoenixdata:/p:ro -v "$PWD":/b alpine tar czf /b/phoenixdata.tgz -C /p .`

Why the working dir is `/mnt/data` (2026-09-12): the image keeps its venv at `/phoenix/.venv` and puts it on
`PYTHONPATH`. With `PHOENIX_WORKING_DIR=/phoenix` the data volume was mounted over it, Docker had copied the 20.8.0 venv
into the empty volume at first start, and that copy shadowed every later image: the 20.11.0 container reported
`Arize Phoenix v20.8.0`. Moving the working dir out of the image's install path fixed it; the stale `.venv` was deleted
from the volume.

## When it is down

Nothing user-facing changes; the collector logs export errors for the Phoenix exporter and keeps sending to Jaeger and
the SIEM file.

## Success example

Scenario 5 trace `79b13369fe3bf8fec4b455f784a0683b`: one LLM span `chat gpt-4o-mini`, 162 prompt tokens / 189 completion
tokens, `llm.invocation_parameters={"temperature": 0.7, "tools": []}`. With content on, the input shows three placeholder
tokens and no name, phone or email.

## Failure example

A `chat` span with status ERROR and an exception event (provider 402/5xx, F11) shows in Phoenix with no `output.value` and
no completion tokens. Hour-one gate (`make gate`, `make check-gate TRACE_ID=...`) asserts the opposite: a chat span with
token counts present in Phoenix, which is the second pass criterion of the design's risk gate.

## Related

- [otel-collector.md](otel-collector.md), [jaeger.md](jaeger.md), [../05-observability-reference.md](../05-observability-reference.md)
