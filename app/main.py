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
from app.config import get_settings
from app.db import check_db

settings = get_settings()

app = FastAPI(title=settings.app_name, version=__version__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
