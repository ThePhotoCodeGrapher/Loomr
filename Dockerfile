# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_HOME=/app \
    MODE=api

WORKDIR ${APP_HOME}

# System deps for pillow, cairosvg, etc.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    libcairo2 \
    libpango-1.0-0 \
    libgdk-pixbuf2.0-0 \
    curl \
  && rm -rf /var/lib/apt/lists/*

# Copy source
COPY messaging_service/ messaging_service/
COPY examples/ examples/
COPY Makefile Makefile

# Install deps
RUN python -m venv /opt/venv \
  && . /opt/venv/bin/activate \
  && pip install --upgrade pip \
  && pip install -r messaging_service/requirements.txt

ENV PATH="/opt/venv/bin:$PATH"

# Expose API port
EXPOSE 8090

# Default entrypoint can run API or BOT depending on MODE env
CMD ["/bin/sh", "-c", "if [ \"$MODE\" = \"bot\" ]; then python messaging_service/main.py; else uvicorn messaging_service.api_server:app --host 0.0.0.0 --port 8090; fi" ]
