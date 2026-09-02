FROM python:3.13-slim AS builder

WORKDIR /app
ENV SERVICE_HOST=service.internal.example
ENV SERVICE_PORT=8080

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && pip install --no-cache-dir common-lib requests

COPY src/ /app/src/

ENTRYPOINT ["python3", "/app/src/app.py"]
