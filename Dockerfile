# syntax=docker/dockerfile:1

# --- builder: resolve deps into a venv with uv ---
FROM python:3.12-slim AS builder
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
FROM python:3.12-slim AS runtime
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
# Multiprocess metrics: every gunicorn worker writes to this shared dir, and /metrics
# aggregates across them (gunicorn.conf.py clears it on start). /tmp is writable by the
# non-root appuser and ephemeral per pod (counters are cumulative-from-process-start).
ENV PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus-multiproc
USER appuser
EXPOSE 8000
# Config lives in gunicorn.conf.py: workers/bind + the multiprocess on_starting/child_exit hooks.
CMD ["gunicorn", "app.main:app", "-c", "gunicorn.conf.py"]
