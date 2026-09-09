"""Project service (S8): create a project from a recommendation pick + list/get.

Owner-scoping lands here for real (the handoff's note): a project is owned by its
creator, and you may only build one from your *own* recommendation option. Missing
references → 404; an option that exists but isn't yours → 403 (require_owner).
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_runtime import require_enabled_runtime_config
from app.auth.deps import require_owner
from app.models import JenkinsConnection, Model, Project, RecommendationOption, User
from app.schemas.project import ProjectCreate, ProjectOut, ProjectUpdate
from app.tasks import CI_REVIEW, is_project_task


def _not_found(what: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found")


def task_type_of_option(option: RecommendationOption) -> str:
    """The task an option was ranked on = its requirements profile's task type (one,
    in practice — the recommender rejects a mixed request). A profile without a
    project task (older rows, `agentic_coding`) falls back to the review task."""
    task_types = option.profile.task_types or []
    return task_types[0] if task_types and is_project_task(task_types[0]) else CI_REVIEW


def _resolve_task_type(option: RecommendationOption, requested: str | None) -> str:
    """E20: a project's task must be the one its selected option was ranked on — an
    option scored on RealVuln cannot back a review project (and vice versa). Omitted
    → derived; given → must match (422 otherwise)."""
    derived = task_type_of_option(option)
    if requested is not None and requested != derived:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"taskType '{requested}' does not match the selected option, "
                f"which was recommended for '{derived}'"
            ),
        )
    return derived


def _to_out(db: Session, project: Project) -> ProjectOut:
    """Enrich a Project with the model names the FE renders (one lookup each)."""
    option = db.get(RecommendationOption, project.selected_option_id)
    selected_model = db.get(Model, option.model_id) if option else None
    baseline = db.get(Model, project.baseline_model_id)
    # Onboarded only once the Jenkins connection exists with a minted CI token (the
    # user finished /ci-setup). Anything short of that is "setup incomplete" (S15d).
    conn = db.scalar(
        select(JenkinsConnection).where(JenkinsConnection.project_id == project.id)
    )
    setup_complete = conn is not None and conn.ci_token_hash is not None
    return ProjectOut(
        id=project.id,
        name=project.name,
        user_id=project.user_id,
        selected_option_id=project.selected_option_id,
        selected_option_model=selected_model.name if selected_model else "",
        baseline_model_id=project.baseline_model_id,
        baseline_model=baseline.name if baseline else "",
        baseline_vendor=baseline.vendor if baseline else "",
        task_type=project.task_type,
        review_preferences=project.review_preferences,
        setup_complete=setup_complete,
    )


def create_project(
    db: Session, payload: ProjectCreate, current_user: User
) -> ProjectOut:
    # The selected option must exist and belong to the caller's own profile.
    option = db.get(RecommendationOption, payload.selected_option_id)
    if option is None:
        raise _not_found("Recommendation option")
    require_owner(option.profile.user_id, current_user)  # 403 if not the caller's
    require_enabled_runtime_config(db, option.model_id, option.model.name)

    # The baseline must be a real model (savings are computed against its price).
    if db.get(Model, payload.baseline_model_id) is None:
        raise _not_found("Baseline model")

    project = Project(
        user_id=current_user.id,
        name=payload.name,
        selected_option_id=payload.selected_option_id,
        baseline_model_id=payload.baseline_model_id,
        task_type=_resolve_task_type(option, payload.task_type),
        review_preferences=payload.review_preferences,
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


def update_project(
    db: Session, project_id: int, payload: ProjectUpdate, current_user: User
) -> ProjectOut:
    """Partial edit (S15d): rename and/or re-pick. Same validation as create —
    a new option must exist AND belong to the caller (403 otherwise); a new
    baseline must be a real model. Omitted fields are left untouched."""
    project = db.get(Project, project_id)
    if project is None:
        raise _not_found("Project")
    require_owner(project.user_id, current_user)  # 403 if not the caller's

    data = payload.model_dump(exclude_unset=True)

    if "selected_option_id" in data:
        option = db.get(RecommendationOption, data["selected_option_id"])
        if option is None:
            raise _not_found("Recommendation option")
        require_owner(option.profile.user_id, current_user)  # only your own option
        require_enabled_runtime_config(db, option.model_id, option.model.name)
        project.selected_option_id = data["selected_option_id"]
        # A re-pick may move the project to the other task; the task follows the
        # option (and an explicit taskType must agree with it).
        project.task_type = _resolve_task_type(option, data.get("task_type"))
    elif "task_type" in data and data["task_type"] is not None:
        # Changing the task without re-picking: it must still match the current option.
        option = db.get(RecommendationOption, project.selected_option_id)
        if option is not None:
            project.task_type = _resolve_task_type(option, data["task_type"])

    if "review_preferences" in data:
        project.review_preferences = data["review_preferences"]  # None clears

    if "baseline_model_id" in data:
        if db.get(Model, data["baseline_model_id"]) is None:
            raise _not_found("Baseline model")
        project.baseline_model_id = data["baseline_model_id"]

    if "name" in data:
        project.name = data["name"]

    db.commit()
    db.refresh(project)
    return _to_out(db, project)


def delete_project(db: Session, project_id: int, current_user: User) -> None:
    """Delete a project + its whole subtree (S15d). The FK cascade
    (migration b1c2d3e4f5a6) removes the Jenkins connection, CI runs/findings/
    feedback, llm_call rows, chat messages/traces and alerts in the DB, so this is a
    single owner-scoped delete."""
    project = db.get(Project, project_id)
    if project is None:
        raise _not_found("Project")
    require_owner(project.user_id, current_user)  # 403 if not the caller's
    db.delete(project)
    db.commit()
