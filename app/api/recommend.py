"""Recommender routes: deterministic pick (S6) + keyword pre-fill (S7).

Both auth-gated. The pick persists a requirements_profile owned by the current
user plus its ranked options + evidence. Pre-fill is a pure helper (no DB, no LLM)
the form uses to suggest fields the user then confirms.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, get_db
from app.models import User
from app.recommend import prefill as prefill_mod
from app.recommend import service
from app.schemas.recommend import (
    PrefillRequest,
    PrefillResult,
    RecommendationRequest,
    RecommendationResult,
)

router = APIRouter(prefix="/recommendations", tags=["recommender"])


@router.post("", response_model=RecommendationResult, status_code=status.HTTP_201_CREATED)
def create_recommendation(
    payload: RecommendationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RecommendationResult:
    return service.recommend(db, payload, current_user)


@router.post("/prefill", response_model=PrefillResult)
def prefill_form(
    payload: PrefillRequest,
    _: User = Depends(get_current_user),
) -> PrefillResult:
    return prefill_mod.prefill(payload.text)
