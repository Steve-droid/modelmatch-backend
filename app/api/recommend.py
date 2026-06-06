"""Recommender routes: deterministic pick (S6).

Auth-gated. The pick persists a requirements_profile owned by the current user
plus its ranked options + evidence. (Keyword pre-fill is added in S7.)
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, get_db
from app.models import User
from app.recommend import service
from app.schemas.recommend import RecommendationRequest, RecommendationResult

router = APIRouter(prefix="/recommendations", tags=["recommender"])


@router.post("", response_model=RecommendationResult, status_code=status.HTTP_201_CREATED)
def create_recommendation(
    payload: RecommendationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RecommendationResult:
    return service.recommend(db, payload, current_user)
