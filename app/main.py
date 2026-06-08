"""ModelMatch backend entrypoint.

S1 platform shell: env-driven config, CORS, and a liveness/readiness split.
Feature stories (recommender, ingestion, chat, savings, agent) extend this.

Run locally:
    uv run uvicorn app.main:app --reload          # dev
    docker compose up                             # backend + Postgres
"""

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import (
    auth,
    benchmarks,
    chat,
    ci,
    findings,
    jenkins,
    projects,
    recommend,
    savings,
)
from app.config import get_settings
from app.db import check_db
from app.observability.metrics import render_metrics

settings = get_settings()

app = FastAPI(title=settings.app_name, version=__version__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(benchmarks.router)
app.include_router(recommend.router)
app.include_router(projects.router)
app.include_router(jenkins.router)
app.include_router(ci.router)
app.include_router(findings.router)
app.include_router(savings.router)
app.include_router(chat.router)


@app.get("/")
def root() -> dict[str, str]:
    return {"app": "modelmatch-backend", "status": "ok", "version": __version__}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the process is up. No dependency checks (never fail on a slow DB)."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz(response: Response) -> dict[str, str]:
    """Readiness: can we serve traffic? Checks the database is reachable."""
    if check_db():
        return {"status": "ready", "db": "ok"}
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "not_ready", "db": "unavailable"}


@app.get("/metrics")
def metrics() -> Response:
    """Prometheus scrape target: per-LLM-call token counters + latency, labeled by
    model + purpose (tokens, NOT dollars — Grafana applies the $/1k rate)."""
    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)
