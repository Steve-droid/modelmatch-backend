"""FastAPI auth dependencies: DB session, current user, owner guard.

- get_current_user resolves a Bearer token to a User → 401 on any failure
  (missing/malformed/expired token, unknown user). One opaque error, no leak.
- require_owner is the owner-scoping guard → 403 when a user touches another
  user's resource. S4 unit-tests it; it wires into real endpoints from S8
  (projects), the first owned resources.
"""

from collections.abc import Generator

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import decode_access_token
from app.ci.tokens import verify_token
from app.db import SessionLocal
from app.models import JenkinsConnection, Project, User

_bearer = HTTPBearer(auto_error=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized
    try:
        payload = decode_access_token(credentials.credentials)
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise unauthorized

    user = db.get(User, user_id)
    if user is None:
        raise unauthorized
    return user


def require_owner(resource_owner_id: int, current_user: User) -> None:
    """Raise 403 unless current_user owns the resource."""
    if resource_owner_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized for this resource",
        )


def require_project_token(
    project_id: int,
    x_ci_token: str | None = Header(default=None, alias="X-CI-Token"),
    db: Session = Depends(get_db),
) -> Project:
    """Authenticate a CI-run ingest by the project's per-project token (NOT a JWT).

    The agent runs in the user's Jenkins and has no user session — it presents the
    minted token in the `X-CI-Token` header. Unknown project → 404; missing,
    unconfigured, or mismatched token → 401 (one opaque error, constant-time).
    """
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing CI token"
    )
    conn = db.scalar(
        select(JenkinsConnection).where(JenkinsConnection.project_id == project_id)
    )
    if conn is None or not conn.ci_token_hash or not x_ci_token:
        raise unauthorized
    if not verify_token(x_ci_token, conn.ci_token_hash):
        raise unauthorized
    return project
