# syntax=docker/dockerfile:1
#
# modelmatch-agent — the REVIEW task image (one LLM call over the PR diff, BYOK).
# The security task ships as its own image: agent/Dockerfile.security.
#
# Build from the REPO ROOT as context (Jenkins agents are amd64; the M4 laptop is arm64):
#     docker buildx build --platform linux/amd64 -f agent/Dockerfile -t modelmatch-agent .
# Run on a diff (stdin), BYOK via env:
#     git diff origin/main... | docker run -i --rm -e LLM_CLIENT=fake modelmatch-agent
#
# Size discipline (measured 2026-09-08; the 1.0.12 image was 291 MB on the same base):
#   * the venv holds ONLY the agent's dependency closure — pydantic(+email) /
#     pydantic-settings / prometheus-client + the three provider SDKs — resolved against
#     the project's frozen lock as CONSTRAINTS, so versions are exactly uv.lock's but the
#     backend-only packages (sqlalchemy, psycopg, alembic, uvicorn, fastapi, sqlglot…)
#     never enter the image;
#   * botocore ships JSON models for ~430 AWS services; the Bedrock adapter uses one —
#     everything except bedrock-runtime / bedrock / sts (+ the top-level files) is dropped;
#   * no bytecode (the agent runs once per CI job; compiling ~0.3 s in memory beats
#     shipping .pyc for every module), no pip/ensurepip (nothing installs at run time),
#     no shell entrypoint (the credential remap is Python), no compilers, no apt packages.
#   * dist-info stays: that is what Trivy reads.

ARG PYTHON_IMAGE=python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

# ---------------------------------------------------------------- builder
FROM ${PYTHON_IMAGE} AS builder
# Same digest the agent pipeline runs uv from (ci/pipeline.agent.env UV_IMAGE, uv 0.9.30).
# In the python-based uv images the binary lives at /usr/local/bin/uv.
COPY --from=ghcr.io/astral-sh/uv:python3.12-bookworm@sha256:85d4cb1afa769a7338e095b927bee941cf5ec92266c7424b3f6c0f2748567248 /usr/local/bin/uv /bin/uv
WORKDIR /build
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=0
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    # 1. the project's frozen resolution (llm extra) as a constraints file
    uv export --frozen --no-dev --extra llm --no-hashes --no-emit-project -o constraints.txt \
    # 2. a venv with the agent's closure only, every version pinned by those constraints
 && uv venv /venv \
 && uv pip install --python /venv -c constraints.txt \
      "pydantic[email]" pydantic-settings prometheus-client \
      anthropic google-genai boto3 cryptography pyasn1 \
    # 3. prune: bytecode, and botocore's service models the agent never loads
 && find /venv -name "__pycache__" -type d -prune -exec rm -rf {} + \
 && cd /venv/lib/python3.12/site-packages/botocore/data \
 && find . -mindepth 1 -maxdepth 1 -type d ! -name bedrock-runtime ! -name bedrock ! -name sts -exec rm -rf {} + \
 && rm -f /venv/lib/python3.12/site-packages/botocore/data/endpoints.json.bak 2>/dev/null; true

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime
# Apply Debian security fixes newer than the pinned Python base (2026-09-12).
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*
# The venv is complete: nothing installs at run time, so the base image's pip/ensurepip
# (and its CVE surface) leave; so does the stdlib bytecode (recompiled in memory once).
RUN rm -rf /usr/local/lib/python3.12/site-packages/pip* /usr/local/lib/python3.12/ensurepip \
           /usr/local/bin/pip* \
 && find /usr/local/lib/python3.12 -name "__pycache__" -type d -prune -exec rm -rf {} + \
 && useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY --from=builder --chown=root:root /venv /venv
# the agent shares app.llm / app.schemas / app.observability with the backend (direct import)
COPY --chown=root:root app ./app
COPY --chown=root:root agent ./agent
# PYTHONSAFEPATH: the review stage runs `-v "$PWD:/work" -w /work`, which made the user's
# checkout sys.path[0] — a top-level app.py (any Flask/FastAPI repo) shadowed the agent's
# own `app` package (measured collisions at 1.0.12: agent, app, json, logging, types).
# With safe-path the cwd is never on sys.path; our packages resolve through PYTHONPATH.
ENV PATH="/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONSAFEPATH=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/appuser \
    AGENT_IMAGE_TASK=review
USER appuser
# never edits the repo: reads a diff, emits JSON on stdout
ENTRYPOINT ["python", "-m", "agent"]
