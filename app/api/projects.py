"""Project routes (S8): create from a pick, list, get-by-id. All auth-gated + owner-scoped.

Later stories hang off /projects/{id} (Jenkins connection, ci-runs, savings, chat).
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, get_db
from app.models import User
from app.projects import service
from app.schemas.project import ProjectCreate, ProjectOut, ProjectUpdate

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ProjectOut:
    return service.create_project(db, payload, current_user)


@router.get("", response_model=list[ProjectOut])
def list_projects(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ProjectOut]:
    return service.list_projects(db, current_user)


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ProjectOut:
    return service.get_project(db, project_id, current_user)


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: int,
    payload: ProjectUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ProjectOut:
    return service.update_project(db, project_id, payload, current_user)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    service.delete_project(db, project_id, current_user)
