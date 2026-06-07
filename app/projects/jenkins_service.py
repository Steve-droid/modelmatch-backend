"""Jenkins connection service (S9): store Jenkins/BYOK secrets as refs.

PUT upserts the single connection per project (owner-scoped). The plaintext token
+ key are written to the SecretStore; only the returned refs are persisted on the
jenkins_connection row. Nothing here logs the secret values. No live Jenkins call
in S9 — status is set to 'configured'.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import require_owner
from app.models import JenkinsConnection, Project, User
from app.schemas.jenkins import JenkinsConnectionOut, JenkinsConnectionUpdate
from app.secret_store import get_secret_store


def _require_owned_project(db: Session, project_id: int, current_user: User) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    require_owner(project.user_id, current_user)  # 403 if not the caller's
    return project


def connect_jenkins(
    db: Session, project_id: int, payload: JenkinsConnectionUpdate, current_user: User
) -> JenkinsConnectionOut:
    _require_owned_project(db, project_id, current_user)

    store = get_secret_store()
    token_ref = store.put(
        f"project/{project_id}/jenkins-token", payload.jenkins_token.get_secret_value()
    )
    key_ref = store.put(
        f"project/{project_id}/model-api-key", payload.model_api_key.get_secret_value()
    )

    conn = db.scalar(
        select(JenkinsConnection).where(JenkinsConnection.project_id == project_id)
    )
    if conn is None:
        conn = JenkinsConnection(project_id=project_id)
        db.add(conn)
    conn.base_url = payload.base_url
    conn.job_name = payload.job_name
    conn.jenkins_token_ref = token_ref  # refs only — never the plaintext
    conn.model_api_key_ref = key_ref
    conn.status = "configured"
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
