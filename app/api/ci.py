"""CI integration routes (S11): the ci-setup snippet + run ingest.

GET /projects/{id}/ci-setup — owner JWT; returns the Jenkins stage snippet (+ the
per-project token, minted once).
GET /projects/{id}/agent-config — authed by the PER-PROJECT TOKEN (E20, HLD §3b.1);
what the agent fetches at run time: task, model runtime config, review preferences.
POST /projects/{id}/ci-runs — authed by the PER-PROJECT TOKEN, not a user JWT (the
agent runs in the user's Jenkins, no user session); persists the run + findings.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, get_db, require_project_token
from app.ci import service
from app.models import Project, User
from app.schemas.agent_config import AgentConfigOut
from app.schemas.ci import CiRunIngest, CiRunOut, CiSetupOut

router = APIRouter(prefix="/projects", tags=["ci"])


@router.get("/{project_id}/ci-setup", response_model=CiSetupOut)
def get_ci_setup(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CiSetupOut:
    return service.ci_setup(db, project_id, current_user)


@router.post("/{project_id}/ci-setup/rotate", response_model=CiSetupOut)
def rotate_ci_token(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CiSetupOut:
    # Recovery for a lost mint-once token: issues a fresh one (the old one stops working).
    return service.rotate_ci_token(db, project_id, current_user)


@router.get("/{project_id}/agent-config", response_model=AgentConfigOut)
def get_agent_config(
    project: Project = Depends(require_project_token),
    db: Session = Depends(get_db),
) -> AgentConfigOut:
    # Same auth as /ci-runs (404 unknown project, 401 bad/missing token) — one path.
    return service.agent_config(db, project)


@router.post(
    "/{project_id}/ci-runs",
    response_model=CiRunOut,
    status_code=status.HTTP_201_CREATED,
)
def ingest_ci_run(
    payload: CiRunIngest,
    project: Project = Depends(require_project_token),
    db: Session = Depends(get_db),
) -> CiRunOut:
    return service.ingest_run(db, project, payload)
