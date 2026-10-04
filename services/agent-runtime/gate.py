"""Hour-one risk gate.

Starts a root span `assistant.run`, opens a hand-rolled `chat <model>` span with gen_ai.* and
OpenInference attributes, and calls LiteLLM through the OpenAI SDK. httpx instrumentation
injects `traceparent`, so LiteLLM's `litellm_request` span must appear as a child of `chat`.

Pass criteria (checked by scripts/check_gate.py against the Jaeger API):
  1. one trace containing services agent-runtime AND litellm
  2. Phoenix shows the chat span with token counts
Prints the trace_id on the last line as `TRACE_ID=<hex>`.
"""
import os
import sys
import time

from openai import OpenAI
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from otel_setup import flush, init_tracing

tracer = init_tracing("agent-runtime")

MODEL = os.environ.get("GATE_MODEL", "gpt-4o-mini")
client = OpenAI(base_url=os.environ["LITELLM_BASE_URL"], api_key=os.environ["LITELLM_KEY"], max_retries=0)

with tracer.start_as_current_span("assistant.run") as root:
    root.set_attribute("openinference.span.kind", "AGENT")
    root.set_attribute("enduser.id", "gate@aicp.test")
    root.set_attribute("tenant_id", "tenant-a")
    root.set_attribute("role", "Finance")
    trace_id = format(root.get_span_context().trace_id, "032x")

    with tracer.start_as_current_span(f"chat {MODEL}") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.system", "litellm")
        span.set_attribute("gen_ai.request.model", MODEL)
        span.set_attribute("openinference.span.kind", "LLM")
        span.set_attribute("llm.model_name", MODEL)
        prompt = "Reply with exactly the word: ok"
        span.set_attribute("input.value", prompt)
        t0 = time.time()
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=5,
                temperature=0,
            )
            text = resp.choices[0].message.content or ""
            usage = resp.usage
            span.set_attribute("gen_ai.response.model", resp.model or MODEL)
            span.set_attribute("gen_ai.response.finish_reasons", [c.finish_reason or "" for c in resp.choices])
            if usage:
                span.set_attribute("gen_ai.usage.input_tokens", usage.prompt_tokens)
                span.set_attribute("gen_ai.usage.output_tokens", usage.completion_tokens)
                span.set_attribute("llm.token_count.prompt", usage.prompt_tokens)
                span.set_attribute("llm.token_count.completion", usage.completion_tokens)
                span.set_attribute("llm.token_count.total", usage.total_tokens)
            span.set_attribute("output.value", text)
            print(f"GATE_CALL_OK model={MODEL} latency_ms={int((time.time()-t0)*1000)} answer={text!r}")
        except Exception as e:  # noqa: BLE001
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)[:200]))
            print(f"GATE_CALL_FAILED {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)

flush()
print(f"TRACE_ID={trace_id}")
