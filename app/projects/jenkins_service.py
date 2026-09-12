"""Jenkins connection service (S9; metadata-only since S15c).

PUT upserts the single connection per project (owner-scoped). The connection is
**metadata only** — base URL + job name. The provider key and the per-project CI
token live in the user's own Jenkins credentials, never in Driftplain, so nothing
secret is collected or stored here. No live Jenkins call in S9 — status is set to
'configured'. (The legacy `*_ref` columns stay nullable + unused; no migration.)
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import require_owner, require_real_project
from app.models import JenkinsConnection, Project, User
from app.schemas.jenkins import JenkinsConnectionOut, JenkinsConnectionUpdate


def _require_owned_project(db: Session, project_id: int, current_user: User) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    require_owner(project.user_id, current_user)  # 403 if not the caller's
    return project


def connect_jenkins(
    db: Session, project_id: int, payload: JenkinsConnectionUpdate, current_user: User
) -> JenkinsConnectionOut:
    require_real_project(_require_owned_project(db, project_id, current_user))

    conn = db.scalar(
        select(JenkinsConnection).where(JenkinsConnection.project_id == project_id)
    )
    if conn is None:
        conn = JenkinsConnection(project_id=project_id)
        db.add(conn)
    conn.base_url = payload.base_url
    conn.job_name = payload.job_name
    conn.status = "configured"  # metadata captured; the CI token is minted at /ci-setup
    db.commit()
    db.refresh(conn)
    return JenkinsConnectionOut.model_validate(conn)


def get_jenkins(
    db: Session, project_id: int, current_user: User
) -> JenkinsConnectionOut:
    _require_owned_project(db, project_id, current_user)
    conn = db.scalar(
        select(JenkinsConnection).where(JenkinsConnection.project_id == project_id)
    )
    if conn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Jenkins connection not found"
        )
    return JenkinsConnectionOut.model_validate(conn)
