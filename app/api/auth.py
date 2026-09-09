"""Auth routes: register, login, and a protected `me`.

JSON in/out (not OAuth2 form) to stay consistent with the camelCase API contract.
Errors: 409 (duplicate email), 401 (bad credentials / invalid token), 422 (schema).
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.auth import google, service
from app.config import get_settings
from app.auth.deps import get_current_user, get_db
from app.auth.security import create_access_token
from app.models import User
from app.schemas.auth import (GoogleChallengeOut, GoogleLoginRequest, LoginRequest, TokenOut, UserCreate, UserOut)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: Session = Depends(get_db)) -> User:
    if service.get_user_by_email(db, payload.email) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        )
    return service.register_user(db, payload.email, payload.password)


@router.post("/login", response_model=TokenOut)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenOut:
    user = service.authenticate_user(db, payload.email, payload.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
        )
    return TokenOut(access_token=create_access_token(str(user.id)))


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user


@router.get("/google/config")
def google_config(response: Response) -> dict[str, bool]:
    response.headers["Cache-Control"] = "no-store"
    return {"enabled": bool(get_settings().google_client_id.strip())}


@router.post("/google/challenge", response_model=GoogleChallengeOut,
             dependencies=[Depends(google.require_google_origin)])
def google_challenge(response: Response) -> GoogleChallengeOut:
    response.headers["Cache-Control"] = "no-store"
    return google.create_challenge()


@router.post("/google", response_model=TokenOut,
             dependencies=[Depends(google.require_google_origin)])
def google_login(payload: GoogleLoginRequest, response: Response,
                 db: Session = Depends(get_db)) -> TokenOut:
    response.headers["Cache-Control"] = "no-store"
    user = google.authenticate_google(db, payload.credential, payload.challenge)
    return TokenOut(access_token=create_access_token(str(user.id)))
