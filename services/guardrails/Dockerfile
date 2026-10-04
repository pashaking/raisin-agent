FROM python:3.12-slim
WORKDIR /app
COPY common/requirements-base.txt requirements.txt
# CPU-only torch keeps the image ~1.5 GB instead of ~6 GB with CUDA wheels.
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir "transformers>=4.51,<5" "sentencepiece" "protobuf" "tiktoken"
COPY common/otel_setup.py .
COPY guardrails/*.py .
ENV PYTHONUNBUFFERED=1 HF_HOME=/cache HF_HUB_DISABLE_TELEMETRY=1 TOKENIZERS_PARALLELISM=false
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
