FROM python:3.12-slim
WORKDIR /app
COPY common/requirements-base.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY common/otel_setup.py .
COPY seed/ .
ENV PYTHONUNBUFFERED=1
CMD ["python", "seed.py", "all"]
