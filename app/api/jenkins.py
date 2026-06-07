"""Jenkins connection routes (S9): connect (BYOK) + view status. Owner-scoped.

Secrets go in as plaintext and are stored as refs (see jenkins_service); the
response exposes refs + status only. The CI-setup snippet + run-ingest land in S11.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, get_db
from app.models import User
from app.projects import jenkins_service as service
from app.schemas.jenkins import JenkinsConnectionOut, JenkinsConnectionUpdate

router = APIRouter(prefix="/projects", tags=["jenkins"])


@router.put("/{project_id}/jenkins", response_model=JenkinsConnectionOut)
def connect_jenkins(
    project_id: int,
    payload: JenkinsConnectionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> JenkinsConnectionOut:
    return service.connect_jenkins(db, project_id, payload, current_user)


@router.get("/{project_id}/jenkins", response_model=JenkinsConnectionOut)
def get_jenkins(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> JenkinsConnectionOut:
    return service.get_jenkins(db, project_id, current_user)
