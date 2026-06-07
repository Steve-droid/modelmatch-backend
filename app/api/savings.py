"""Savings dashboard routes (S14): the read behind the centerpiece dashboard.

- GET /projects/{id}/savings?range= → `{ kpis, series[], runs[] }` (architecture §6).
- GET /projects/{id}/runs/{run_id}/findings → the runs-table drill-in (Fork 4).

Both are user-JWT + owner-scoped (a USER viewing their own spend), NOT the per-project
CI-token path (that's agent ingest only). Pure reads — DETERMINISTIC, no LLM, zero
tokens. `range` is a Literal so a bad value is a clean 422.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, get_db
from app.models import User
from app.savings import dashboard
from app.schemas.savings import RunFindingsResponse, SavingsRange, SavingsResponse

router = APIRouter(prefix="/projects", tags=["savings"])


@router.get("/{project_id}/savings", response_model=SavingsResponse)
def get_project_savings(
    project_id: int,
    range: SavingsRange = Query(default="all"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SavingsResponse:
    return dashboard.project_savings(db, project_id, current_user, range)


@router.get(
    "/{project_id}/runs/{run_id}/findings", response_model=RunFindingsResponse
)
def get_run_findings(
    project_id: int,
    run_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RunFindingsResponse:
    return dashboard.run_findings(db, project_id, run_id, current_user)
