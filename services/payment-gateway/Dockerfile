FROM python:3.12-slim
WORKDIR /app
COPY common/requirements-base.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY common/otel_setup.py .
COPY payment-gateway/app.py .
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8070"]
