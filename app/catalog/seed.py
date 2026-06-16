"""Static catalog seed loader — idempotent by construction (upsert per row)."""

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.service import upsert_catalog_row
from app.models import AgentRuntimeConfig, Model
from app.schemas.catalog import CatalogRowIn

SEED_PATH = Path(__file__).resolve().parent / "seed_data.json"


def _upsert_runtime_config(db: Session, raw: dict) -> None:
    """Upsert trusted static CI-agent runtime metadata for an already-seeded model."""
    model = db.scalar(
        select(Model).where(Model.name == raw["model"], Model.vendor == raw["vendor"])
    )
    if model is None:
        raise ValueError(
            "agent runtime config references unknown model "
            f"{raw['vendor']}/{raw['model']}"
        )

    config = db.scalar(
        select(AgentRuntimeConfig).where(AgentRuntimeConfig.model_id == model.id)
    )
    values = {
        "provider": raw["provider"],
        "provider_model_id": raw["provider_model_id"],
        "auth_mode": raw["auth_mode"],
        "credential_env_var": raw.get("credential_env_var"),
        "enabled": raw.get("enabled", True),
    }
    if config is None:
        db.add(AgentRuntimeConfig(model_id=model.id, **values))
        return

    for key, value in values.items():
        setattr(config, key, value)


def load_seed(db: Session, path: Path = SEED_PATH) -> int:
    """Upsert every row in the seed file. Returns the number of rows processed.

    Safe to run repeatedly: each row upserts on its natural key, so re-running
    refreshes figures without creating duplicates.
    """
    data = json.loads(path.read_text())
    rows = data["benchmark_results"]
    for raw in rows:
        upsert_catalog_row(db, CatalogRowIn.model_validate(raw))
    for raw in data.get("agent_runtime_configs", []):
        _upsert_runtime_config(db, raw)
    db.commit()
    return len(rows)
