"""ModelMatch backend — stub entrypoint.

Minimal runnable scaffold so the first feature story has something to extend.
The real app (/healthz + /readyz split, DB, recommender, ingestion, chat, savings)
lands with story S1 onward.

Run locally:
    uv run uvicorn app.main:app --reload
"""

from fastapi import FastAPI

from app import __version__

app = FastAPI(title="ModelMatch backend", version=__version__)


@app.get("/")
def root() -> dict[str, str]:
    return {"app": "modelmatch-backend", "status": "scaffold", "version": __version__}
