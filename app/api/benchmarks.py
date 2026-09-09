"""Catalog routes: list and add benchmark rows, plus LLM ingestion (#3).

The catalog is GLOBAL reference data (not owner-scoped). Reads require login;
writes and paid ingestion require the operator permission. POST upserts on the
row's natural key (idempotent).
`POST /benchmarks/ingest` (S5b) runs the in-cluster LLM over an unstructured source
to fill the catalog — idempotent by content hash, hourly-token-capped (429), and
validated as untrusted before any row is persisted.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, get_db, require_operator
from app.catalog import service
from app.catalog.service import TaskBenchmarkConflict
from app.ingest.service import build_ingest_client, ingest_source
from app.llm import LLMClient
from app.llm_budget import HourlyTokenCapExceeded
from app.models import User
from app.schemas.catalog import CatalogRowIn, CatalogRowOut
from app.schemas.ingest import IngestRequest, IngestResult

router = APIRouter(prefix="/benchmarks", tags=["catalog"])


@router.get("", response_model=list[CatalogRowOut])
def list_benchmarks(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[CatalogRowOut]:
    return service.list_catalog(db)


@router.post("", response_model=CatalogRowOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_operator)])
def add_benchmark(
    payload: CatalogRowIn,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> CatalogRowOut:
    try:
        return service.upsert_catalog_row(db, payload)
    except TaskBenchmarkConflict as exc:
        # The one-(benchmark, metric)-per-task-type invariant (P38c). 422: the row is
        # well-formed but not storable against the catalog's current task model.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


@router.post("/ingest", response_model=IngestResult, status_code=status.HTTP_200_OK,
             dependencies=[Depends(require_operator)])
def ingest_benchmarks(
    payload: IngestRequest,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    client: LLMClient = Depends(build_ingest_client),
) -> IngestResult:
    """LLM-ingest an unstructured source into the catalog (#3). 200 (not 201): an
    ingest may create zero rows (unchanged source → skipped, or all rows rejected)."""
    try:
        return ingest_source(db, payload, client)
    except HourlyTokenCapExceeded as exc:
        # The hard hourly token cap ABORTS (not just alerts) — surface it as 429.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
        ) from exc
