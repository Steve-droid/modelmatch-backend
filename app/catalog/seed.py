"""Static catalog seed loader — idempotent by construction (upsert per row)."""

import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.catalog.service import upsert_catalog_row
from app.schemas.catalog import CatalogRowIn

SEED_PATH = Path(__file__).resolve().parent / "seed_data.json"


def load_seed(db: Session, path: Path = SEED_PATH) -> int:
    """Upsert every row in the seed file. Returns the number of rows processed.

    Safe to run repeatedly: each row upserts on its natural key, so re-running
    refreshes figures without creating duplicates.
    """
    data = json.loads(path.read_text())
    rows = data["benchmark_results"]
    for raw in rows:
        upsert_catalog_row(db, CatalogRowIn.model_validate(raw))
    return len(rows)
