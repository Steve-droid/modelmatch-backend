"""Project service (S8): create a project from a recommendation pick + list/get.

Owner-scoping lands here for real (the handoff's note): a project is owned by its
creator, and you may only build one from your *own* recommendation option. Missing
references → 404; an option that exists but isn't yours → 403 (require_owner).
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import require_owner
from app.models import Model, Project, RecommendationOption, User
from app.schemas.project import ProjectCreate, ProjectOut


def _not_found(what: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")


def _to_out(db: Session, project: Project) -> ProjectOut:
    """Enrich a Project with the model names the FE renders (one lookup each)."""
    option = db.get(RecommendationOption, project.selected_option_id)
    selected_model = db.get(Model, option.model_id) if option else None
    baseline = db.get(Model, project.baseline_model_id)
    return ProjectOut(
        id=project.id,
        name=project.name,
        user_id=project.user_id,
        selected_option_id=project.selected_option_id,
        selected_option_model=selected_model.name if selected_model else "",
        baseline_model_id=project.baseline_model_id,
        baseline_model=baseline.name if baseline else "",
        baseline_vendor=baseline.vendor if baseline else "",
    )


def create_project(
    db: Session, payload: ProjectCreate, current_user: User
) -> ProjectOut:
    # The selected option must exist and belong to the caller's own profile.
    option = db.get(RecommendationOption, payload.selected_option_id)
    if option is None:
        raise _not_found("Recommendation option")
    require_owner(option.profile.user_id, current_user)  # 403 if not the caller's

    # The baseline must be a real model (savings are computed against its price).
    if db.get(Model, payload.baseline_model_id) is None:
        raise _not_found("Baseline model")

    project = Project(
        user_id=current_user.id,
        name=payload.name,
        selected_option_id=payload.selected_option_id,
        baseline_model_id=payload.baseline_model_id,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return _to_out(db, project)


def list_projects(db: Session, current_user: User) -> list[ProjectOut]:
    projects = db.scalars(
        select(Project).where(Project.user_id == current_user.id).order_by(Project.id)
    ).all()
    return [_to_out(db, p) for p in projects]


def get_project(db: Session, project_id: int, current_user: User) -> ProjectOut:
    project = db.get(Project, project_id)
    if project is None:
        raise _not_found("Project")
    require_owner(project.user_id, current_user)  # 403 if not the caller's
    return _to_out(db, project)
