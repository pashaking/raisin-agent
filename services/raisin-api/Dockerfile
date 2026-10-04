FROM python:3.12-slim
WORKDIR /app
COPY common/requirements-base.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt "PyJWT[crypto]>=2.9,<3"
COPY common/otel_setup.py .
COPY raisin-api/app.py raisin-api/db.py raisin-api/keys.py .
COPY raisin-api/ui ./ui
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
