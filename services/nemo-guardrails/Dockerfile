FROM python:3.12-slim
WORKDIR /app
# nemoguardrails server needs fastapi/uvicorn explicitly in 0.24; yara-python backs the `injection detection` rail.
RUN pip install --no-cache-dir "nemoguardrails==0.24.0" "fastapi>=0.115,<1" "uvicorn[standard]>=0.30,<1" "yara-python" "langchain-openai" \
 && pip install --no-cache-dir opentelemetry-distro opentelemetry-exporter-otlp-proto-http \
      opentelemetry-instrumentation-fastapi opentelemetry-instrumentation-httpx opentelemetry-instrumentation-requests
COPY nemo-guardrails/config /config
ENV PYTHONUNBUFFERED=1
# opentelemetry-instrument gives the server FastAPI spans (nested under the guardrails service via traceparent) and
# httpx/requests client spans for the judge calls to LiteLLM, without touching NeMo's code.
CMD ["opentelemetry-instrument", "nemoguardrails", "server", "--config", "/config", "--port", "8010", "--disable-chat-ui", "--default-config-id", "aicp"]
