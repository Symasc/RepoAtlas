FROM python:3.13-slim AS builder

ENV SERVICE_PORT=8080
ARG BUILD_REVISION

WORKDIR /app
COPY src/ /app/src/
RUN pip install --no-cache-dir requests

ENTRYPOINT ["python3", "/app/src/app.py"]
