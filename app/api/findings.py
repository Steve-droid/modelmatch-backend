"""Finding-feedback route (S13): the quality signal.

POST /findings/{id}/feedback — user-JWT, owner-scoped via the finding → run →
project → user chain. Records an accept/reject verdict and recomputes the affected
run's `quality_ok`. DETERMINISTIC (no LLM) — the verdict is a human judgement, the
gate is arithmetic.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, get_db
from app.models import User
from app.quality import service
from app.schemas.feedback import FeedbackIn, FeedbackOut

router = APIRouter(prefix="/findings", tags=["findings"])


@router.post("/{finding_id}/feedback", response_model=FeedbackOut)
def submit_finding_feedback(
    finding_id: int,
    payload: FeedbackIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FeedbackOut:
    return service.submit_feedback(db, finding_id, payload, current_user)
