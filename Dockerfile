# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

# --- builder: resolve deps into a venv with uv ---
FROM ${PYTHON_IMAGE} AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Install dependencies first (cached) without the project, then the project.
# --extra bedrock pulls boto3 so the in-cluster LLM surface (ingestion #3 + chat #4)
# can call Bedrock Nova via IRSA. ONLY boto3 (the two-surface rule) — the BYOK
# anthropic/gemini SDKs live in the agent image's `--extra llm`, never here.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra bedrock
COPY app ./app
RUN uv sync --frozen --no-dev --extra bedrock

# --- runtime: slim, non-root, gunicorn+uvicorn (no --reload) ---
FROM ${PYTHON_IMAGE} AS runtime
# Apply Debian security fixes newer than the pinned Python base (2026-09-12).
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY --from=builder --chown=appuser:appuser /app /app
# Ship the Alembic migrations + config IN the image so the SAME image runs the schema
# step — a one-off `alembic upgrade head` (the K8s migrate-Job / the compose `migrate`
# service), never on serve-startup. CMD below stays gunicorn-only.
COPY --chown=appuser:appuser migrations ./migrations
COPY --chown=appuser:appuser alembic.ini ./
COPY --chown=appuser:appuser gunicorn.conf.py ./
ENV PATH="/app/.venv/bin:$PATH"
USER appuser
EXPOSE 8000
# Config lives in gunicorn.conf.py: workers/bind + the multiprocess wiring. The
# PROMETHEUS_MULTIPROC_DIR env is set INSIDE that file (scoped to the gunicorn process
# tree) — deliberately NOT an image-wide ENV, so `alembic upgrade head` on the SAME
# image keeps the plain in-process registry instead of crashing on a missing mmap dir.
CMD ["gunicorn", "app.main:app", "-c", "gunicorn.conf.py"]
