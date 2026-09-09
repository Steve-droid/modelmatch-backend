"""Private sample projects, created atomically with a new account. No provider calls."""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import Project, User
from app.projects.service import create_project
from app.recommend.service import recommend
from app.schemas.project import ProjectCreate
from app.schemas.recommend import RecommendationRequest
from app.tasks import CI_REVIEW, SECURITY_ANALYSIS

EXAMPLE_PROJECTS = (
    (CI_REVIEW, "Example: Pull Request Review", 30),
    (SECURITY_ANALYSIS, "Example: Security Scan", 20),
)


def provision_examples(db: Session, user: User) -> None:
    """Caller owns the transaction; no service may commit account/partial examples.

    Only called for a newly inserted account. The identity index and lookup make an
    internal retry safe without relying on editable names. Login never restores a
    deleted example. Existing catalog rows are read, never seeded/overwritten here.
    """
    # Lazy import: the operator CLI's seeder also uses the auth registration service.
    from app.demo.seed import seed_runs, _REVIEW_TOKENS, _SECURITY_TOKENS, _SECURITY_FINDINGS

    try:
        for task, name, count in EXAMPLE_PROJECTS:
            existing = db.scalar(select(Project.id).where(
                Project.user_id == user.id, Project.is_example.is_(True), Project.task_type == task,
            ))
            if existing is not None:
                continue
            pick = recommend(db, RecommendationRequest(task_types=[task], budget_sensitivity="high"),
                             user, commit=False)
            created = create_project(db, ProjectCreate(
                name=name, selected_option_id=pick.suggested.recommendation_option_id,
                baseline_model_id=pick.baseline.model_id, task_type=task,
            ), user, commit=False)
            project = db.get(Project, created.id)
            project.is_example = True
            db.flush()
            seed_runs(db, project, count, task=task,
                      tokens=_SECURITY_TOKENS if task == SECURITY_ANALYSIS else _REVIEW_TOKENS,
                      findings=_SECURITY_FINDINGS if task == SECURITY_ANALYSIS else None,
                      commit=False)
    except (HTTPException, SQLAlchemyError):
        raise HTTPException(503, "Example projects are temporarily unavailable. Please try again.") from None
