"""Chat + embeddings through the LiteLLM gateway with hand-rolled gen_ai.* / OpenInference spans.
The OpenAI SDK's httpx client carries `traceparent`, so LiteLLM's own spans nest under `chat <model>`."""
import hashlib
import json

from openai import NOT_GIVEN, OpenAI
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

import config

tracer = trace.get_tracer("agent-runtime.llm")


def content_attr(payload, **structure) -> str:
    """Span attribute for prompt/response/tool-arg content. Full body only with DEBUG_TRACE_CONTENT=on; otherwise the
    structure the trace needs (counts, roles, names) plus a sha256 so two requests can still be compared. Tenant KB
    chunks, tool results and user text never reach Jaeger/Phoenix by default (CSO F1)."""
    raw = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    if config.TRACE_CONTENT:
        return raw[:8000]
    return json.dumps({**structure, "chars": len(raw), "sha256": hashlib.sha256(raw.encode()).hexdigest()[:16], "content": "redacted"})


def client_for(key: str) -> OpenAI:
    return OpenAI(base_url=config.LITELLM_BASE_URL, api_key=key, max_retries=0, timeout=90)


def chat(client: OpenAI, model: str, messages: list[dict], tools: list[dict] | None, temperature: float | None):
    with tracer.start_as_current_span(f"chat {model}") as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.system", "litellm")
        span.set_attribute("gen_ai.request.model", model)
        if temperature is not None:
            span.set_attribute("gen_ai.request.temperature", temperature)
        span.set_attribute("openinference.span.kind", "LLM")
        span.set_attribute("llm.model_name", model)
        span.set_attribute("llm.invocation_parameters", json.dumps({"temperature": temperature, "tools": [t["function"]["name"] for t in (tools or [])]}))
        span.set_attribute("input.value", content_attr(messages, messages=len(messages), roles=[m.get("role", "?") for m in messages],
                                                        tool_results=sum(1 for m in messages if m.get("role") == "tool")))
        span.set_attribute("input.mime_type", "application/json")
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, tools=tools or NOT_GIVEN,
                temperature=temperature if temperature is not None else NOT_GIVEN, max_tokens=600)
        except Exception as e:  # noqa: BLE001
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)[:200]))
            raise
        msg = resp.choices[0].message
        span.set_attribute("gen_ai.response.model", resp.model or model)
        span.set_attribute("gen_ai.response.finish_reasons", [c.finish_reason or "" for c in resp.choices])
        if resp.usage:
            span.set_attribute("gen_ai.usage.input_tokens", resp.usage.prompt_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", resp.usage.completion_tokens)
            span.set_attribute("llm.token_count.prompt", resp.usage.prompt_tokens)
            span.set_attribute("llm.token_count.completion", resp.usage.completion_tokens)
            span.set_attribute("llm.token_count.total", resp.usage.total_tokens)
        if msg.tool_calls:
            span.set_attribute("output.value", content_attr([{"name": tc.function.name, "arguments": tc.function.arguments} for tc in msg.tool_calls],
                                                             tool_calls=[tc.function.name for tc in msg.tool_calls]))
            span.set_attribute("gen_ai.tool.calls", [tc.function.name for tc in msg.tool_calls])
        else:
            span.set_attribute("output.value", content_attr(msg.content or "", kind="text"))
        return msg, resp


def embed(client: OpenAI, text: str) -> list[float]:
    with tracer.start_as_current_span(f"embeddings {config.EMBED_MODEL}") as span:
        span.set_attribute("gen_ai.operation.name", "embeddings")
        span.set_attribute("gen_ai.request.model", config.EMBED_MODEL)
        span.set_attribute("openinference.span.kind", "EMBEDDING")
        r = client.embeddings.create(model=config.EMBED_MODEL, input=text)
        if r.usage:
            span.set_attribute("gen_ai.usage.input_tokens", r.usage.prompt_tokens)
        return r.data[0].embedding
