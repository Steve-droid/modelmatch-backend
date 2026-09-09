"""Modicum backend entrypoint.

S1 platform shell: env-driven config, CORS, and a liveness/readiness split.
Feature stories (recommender, ingestion, chat, savings, agent) extend this.

Run locally:
    uv run uvicorn app.main:app --reload          # dev
    docker compose up                             # backend + Postgres
"""

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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
from app.observability import configure_logging
from app.observability.http import MetricsMiddleware
from app.observability.metrics import render_metrics

# Route modelmatch.* logs to stdout at INFO so the per-request llm_call line actually
# reaches the container's stdout for Fluent Bit/EFK (otherwise INFO is dropped). Runs at
# import → once per gunicorn worker; idempotent.
configure_logging()

settings = get_settings()

app = FastAPI(title=settings.app_name, version=__version__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Added last → outermost in the stack, so it times the full request (incl. CORS).
app.add_middleware(MetricsMiddleware)

app.include_router(auth.router)
app.include_router(benchmarks.router)
app.include_router(recommend.router)
app.include_router(projects.router)
app.include_router(jenkins.router)
app.include_router(ci.router)
app.include_router(findings.router)
app.include_router(savings.router)
app.include_router(chat.router)


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 responses without echoing the submitted value back.

    FastAPI's default handler reflects each rejected field's `input` into the error
    body. For our `extra="forbid"` schemas that means a stray `jenkinsToken` /
    `modelApiKey` (jenkins) or a raw `diff` (ci-run ingest) would be mirrored straight
    back — violating the project's secret/diff-hygiene rule. Strip `input` (and `ctx`,
    which can carry the offending value) from every error so we keep the useful
    type/location/message but never reflect the value. Applies to ALL routes.
    """
    safe = [
        {k: v for k, v in err.items() if k not in ("input", "ctx")}
        for err in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": safe})  # 422; constant is deprecated


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
    """Prometheus scrape target. Three families:
    - HTTP: request count + duration by method/route/status (rate, latency, error rate);
    - DB: query duration by SQL operation;
    - LLM: per-call token counters + latency by model+purpose (tokens, NOT dollars —
      Grafana applies the $/1k rate).
    Aggregated across gunicorn workers when multiprocess mode is on."""
    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)
