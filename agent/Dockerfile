# syntax=docker/dockerfile:1
#
# ModelMatch CI code-review agent image (the product's proof).
# Build from the REPO ROOT as context:
#     docker build -f agent/Dockerfile -t modelmatch-agent .
# Run on a diff (stdin), BYOK via env:
#     git diff origin/main... | docker run -i --rm -e LLM_CLIENT=fake modelmatch-agent

# --- builder: resolve deps into a venv with uv ---
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
# --extra llm pulls the real provider SDKs (anthropic/gemini/bedrock) the agent needs.
RUN uv sync --frozen --no-dev --extra llm --no-install-project
# the agent shares app.llm + the findings contract (direct import, no drift)
COPY app ./app
COPY agent ./agent
RUN uv sync --frozen --no-dev --extra llm

# --- runtime: slim, non-root; reads diff on stdin, writes findings JSON on stdout ---
FROM python:3.12-slim AS runtime
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY --from=builder --chown=appuser:appuser /app /app
ENV PATH="/app/.venv/bin:$PATH"
USER appuser
# never edits the repo: no writable mounts needed; entrypoint only reads + emits JSON
ENTRYPOINT ["python", "-m", "agent"]
