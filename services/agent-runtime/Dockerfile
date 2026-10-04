FROM python:3.12-slim
WORKDIR /app
COPY common/requirements-base.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt "openai>=1.50,<2" "PyJWT[crypto]>=2.9,<3"
COPY common/otel_setup.py .
COPY agent-runtime/*.py .
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090"]
