"""Catalog routes: list and add benchmark rows.

The catalog is GLOBAL reference data (not owner-scoped) but auth-gated, consistent
with the rest of the API. POST upserts on the row's natural key (idempotent).
LLM ingestion of unstructured sources is a separate endpoint (S5b).
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, get_db
from app.catalog import service
from app.models import User
from app.schemas.catalog import CatalogRowIn, CatalogRowOut

router = APIRouter(prefix="/benchmarks", tags=["catalog"])


@router.get("", response_model=list[CatalogRowOut])
def list_benchmarks(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[CatalogRowOut]:
    return service.list_catalog(db)


@router.post("", response_model=CatalogRowOut, status_code=status.HTTP_201_CREATED)
def add_benchmark(
    payload: CatalogRowIn,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> CatalogRowOut:
    return service.upsert_catalog_row(db, payload)
