# otel-collector

The OpenTelemetry Collector (contrib distribution). Every service exports to it; it fans out to Jaeger and Phoenix and
forks security-relevant spans to a file that stands in for a SIEM. Design box: **AI Observability (OTel GenAI traces →
New Relic / SIEM)**.

| | |
|---|---|
| Image | `otel/opentelemetry-collector-contrib:0.160.0` |
| Ports | 14317 → 4317 (OTLP gRPC), 14318 → 4318 (OTLP HTTP) |
| Depends on | `jaeger`, `phoenix` |
| Config | `observability/otel-collector.yaml` (read-only mount) |
| Output file | `observability/siem/security-events.jsonl` (bind mount `./observability/siem:/siem`) |

## Pipelines

```yaml
receivers:  otlp { grpc :4317, http :4318 }
processors:
  batch: {}
  filter/drop_raw_gateway_span:   # F14: LiteLLM's raw provider echo (request params, messages, choices, embedding inputs)
    traces.span: ['name == "raw_gen_ai_request"']
  attributes/no_message_content:  # F14 backstop: delete gen_ai.(input|output).messages, gen_ai.(prompt|completion).N.*,
    actions: [...]                #   llm.(input|output)_messages, llm.<provider>.(messages|choices|input|data)
  filter/security:            # the filter processor DROPS matching spans, so keep-only is "not (...)"
    traces.span:
      - 'not (attributes["policy.result"] == "deny" or attributes["guardrail.verdict"] == "block" or attributes["guardrail.verdict"] == "flagged")'
exporters:
  otlp_grpc/jaeger  { endpoint: jaeger:4317, tls.insecure }
  otlp_http/phoenix { endpoint: http://phoenix:6006 }
  file/siem         { path: /siem/security-events.jsonl }
pipelines:
  traces/all:  otlp → drop_raw_gateway_span, no_message_content, batch → jaeger, phoenix
  traces/siem: otlp → filter/security, drop_raw_gateway_span, no_message_content, batch → file/siem
```

The two F14 processors are the second layer behind LiteLLM's `turn_off_message_logging`; they never touch the runtime's
`input.value` / `output.value` / `tool.parameters`, which stay gated in code by `DEBUG_TRACE_CONTENT`. Note: the file
exporter truncates `security-events.jsonl` when the collector (re)starts.

Who sends what: Python services via OTLP/HTTP (`OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318`), LiteLLM via its
`otel` callback (HTTP), OPA via `distributed_tracing` (gRPC :4317), NeMo via `opentelemetry-instrument` (OTLP). All carry
the resource attribute `openinference.project.name=ai-control-plane-poc` so Phoenix groups them into one project.

## The SIEM file

Raw OTLP JSON, one export batch per line, containing only the spans that passed the filter (their parents are not
included; correlate by `traceId`). `scripts/siem_tail.py` flattens it:

```bash
python3 scripts/siem_tail.py        # dump
python3 scripts/siem_tail.py -f     # follow
: > observability/siem/security-events.jsonl   # reset before a clean demo
```

Spans that land here, by source: `policy.decide` with `policy.result=deny` (runtime), `raisin-api /api/*` role denies
(they set `policy.result=deny` on purpose), `guardrail.input` / `guardrail.output` with `verdict=block`,
`guardrail.scan.*` with `block` or `flagged`, `guardrail.retrieved` with `flagged`.

## When it is down

Requests are unaffected: the SDK batch processors retry then drop. You lose traces for the outage window, and the SIEM
file stops growing. Jaeger and Phoenix show nothing new. Nothing fails closed on observability, by design (the design
document's premise is that controls, not telemetry, gate requests).

## Success example (of the filter)

After the 2026-09-07 capture session, `siem_tail.py` listed eight rows: the injection block (F3), the model deny (F2), the
Donor-on-finance-route deny (F5), four flagged retrieval spans from scenario 4, and the OPA-unavailable deny (F9). None of
the six clean requests (S1, S2, S5, S7, S9, S0) produced a row. Full listing in
[../04-failure-paths.md](../04-failure-paths.md#what-the-siem-feed-looked-like-after-this-session).

## Failure example

`docker compose logs otel-collector` is where exporter errors appear (Jaeger or Phoenix refusing connections, file write
errors). A stale line from before the LLM Guard removal is still in the SIEM file (`guardrail.llm_guard.retrieved_chunk`,
`guardrail.service=llm-guard`), which is a reminder that the file is append-only and should be truncated before a demo.

## Related

- Span catalogue: [../05-observability-reference.md](../05-observability-reference.md)
- [jaeger.md](jaeger.md), [phoenix.md](phoenix.md)
