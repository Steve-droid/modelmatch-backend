"""Trusted CI-agent runtime config lookup.

This is app-owned runtime metadata, not LLM-ingested catalog data. A selected
catalog model may only be turned into a CI agent when it has an enabled runtime
configuration row.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentRuntimeConfig


def runtime_config_error(model_name: str | None = None) -> HTTPException:
    detail = "Selected model has no enabled CI runtime config."
    if model_name:
        detail = f"Selected model '{model_name}' has no enabled CI runtime config."
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def get_enabled_runtime_config(db: Session, model_id: int) -> AgentRuntimeConfig | None:
    return db.scalar(
        select(AgentRuntimeConfig).where(
            AgentRuntimeConfig.model_id == model_id,
            AgentRuntimeConfig.enabled.is_(True),
        )
    )


def require_enabled_runtime_config(db: Session, model_id: int, model_name: str | None = None) -> AgentRuntimeConfig:
    config = get_enabled_runtime_config(db, model_id)
    if config is None:
        raise runtime_config_error(model_name)
    return config
