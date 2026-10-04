"""OTel bootstrap shared by every Python service (copied into each image at build time).

Hand-rolled spans only. httpx instrumentation is enabled so `traceparent` is injected into
outbound calls (OpenAI SDK -> LiteLLM, runtime -> OPA / guardrails / raisin-api);
header and body capture stay off (PII discipline).
"""
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def init_tracing(service_name: str | None = None) -> trace.Tracer:
    name = service_name or os.environ.get("OTEL_SERVICE_NAME", "service")
    attrs = {"service.name": name}
    for kv in os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "").split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            attrs[k.strip()] = v.strip()
    provider = TracerProvider(resource=Resource.create(attrs))
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    return trace.get_tracer(name)


def instrument_fastapi(app) -> None:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    # exclude_spans: no ASGI `http send` / `http receive` sub-spans (pure noise in Phoenix / Jaeger; the server span stays)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,healthz", exclude_spans=["receive", "send"])


def current_trace_id() -> str:
    return format(trace.get_current_span().get_span_context().trace_id, "032x")


def flush() -> None:
    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()
