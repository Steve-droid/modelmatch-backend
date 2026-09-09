"""Google Identity Services: verify ID tokens, then mint our ordinary session.

The browser keeps a five-minute signed challenge in memory and asks GIS to put its
nonce in the Google-signed credential. JSON + an exact allowed Origin prevent login
CSRF. A unique DB nonce insert prevents successful credentials being replayed across
workers. No Google access/refresh tokens, client secret, or cookies are needed.
"""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import secrets

import jwt
from fastapi import HTTPException, Request
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.auth.admission import lock_registration, require_capacity
from app.models import User
from app.models.orm import GoogleLoginNonce
from app.schemas.auth import GoogleChallengeOut

_AUDIENCE = "modicum:google-login"
_EMAIL = TypeAdapter(EmailStr)


def require_google_origin(request: Request) -> None:
    settings = get_settings()
    if not settings.google_client_id.strip():
        raise HTTPException(503, "Google sign-in is not configured")
    # Reject missing/null origins and wildcard config too. A cross-site HTML form
    # cannot submit this JSON endpoint; browser JS must also pass CORS preflight.
    if request.headers.get("origin") not in settings.cors_allow_origins:
        raise HTTPException(403, "Sign-in origin is not allowed")
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "Sign-in requires JSON")


def create_challenge() -> GoogleChallengeOut:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    nonce = secrets.token_urlsafe(32)
    challenge = jwt.encode(
        {"aud": _AUDIENCE, "nonce": nonce, "iat": now, "exp": now + timedelta(minutes=5)},
        settings.jwt_secret, algorithm="HS256",
    )
    return GoogleChallengeOut(client_id=settings.google_client_id, nonce=nonce, challenge=challenge)


@lru_cache
def _google_keys() -> jwt.PyJWKClient:
    # Fixed Google endpoint, bounded network wait, in-memory public-key cache.
    return jwt.PyJWKClient("https://www.googleapis.com/oauth2/v3/certs", timeout=5, lifespan=300)


def verify_credential(credential: str, nonce: str) -> dict:
    client_id = get_settings().google_client_id
    key = _google_keys().get_signing_key_from_jwt(credential)
    claims = jwt.decode(
        credential, key.key, algorithms=["RS256"], audience=client_id,
        issuer=["accounts.google.com", "https://accounts.google.com"],
        options={"require": ["iss", "aud", "exp", "iat", "sub", "email", "email_verified", "nonce"]},
    )
    if (claims["nonce"] != nonce or claims["email_verified"] is not True
            or not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 255
            or claims.get("azp", client_id) != client_id):
        raise jwt.InvalidTokenError("Invalid Google identity")
    claims["email"] = str(_EMAIL.validate_python(claims["email"]))
    return claims


def authenticate_google(db: Session, credential: str, challenge: str) -> User:
    try:
        state = jwt.decode(
            challenge, get_settings().jwt_secret, algorithms=["HS256"], audience=_AUDIENCE,
            options={"require": ["aud", "nonce", "iat", "exp"]},
        )
        claims = verify_credential(credential, state["nonce"])
    except jwt.PyJWKClientConnectionError:
        raise HTTPException(503, "Google sign-in is temporarily unavailable") from None
    except (jwt.PyJWTError, ValidationError, ValueError, TypeError, KeyError):
        raise HTTPException(401, "Google sign-in could not be verified. Please try again.") from None

    # Only validated credentials reach the DB. Clean expired entries; single-use
    # insertion and account creation commit together (concurrent inserts serialize).
    db.execute(delete(GoogleLoginNonce).where(GoogleLoginNonce.expires_at < func.now()))
    consumed = db.scalar(insert(GoogleLoginNonce).values(
        nonce=state["nonce"], expires_at=datetime.fromtimestamp(state["exp"], timezone.utc),
    ).on_conflict_do_nothing().returning(GoogleLoginNonce.nonce))
    if consumed is None:
        db.rollback()
        raise HTTPException(401, "Google sign-in expired or was already used. Please try again.")

    # Stable Google sub is the identity, not a mutable email address. Do not update
    # emails on returning sign-in or silently attach to a legacy password account.
    user = db.scalar(select(User).where(User.google_subject == claims["sub"]))
    created_user_id = None
    if user is None:
        lock_registration(db)
        # Another login may have created this subject while this transaction waited.
        user = db.scalar(select(User).where(User.google_subject == claims["sub"]))
    if user is None:
        existing = db.scalar(select(User).where(func.lower(User.email) == claims["email"].lower()))
        if existing is None:
            require_capacity(db)
            created_user_id = db.scalar(insert(User).values(
                email=claims["email"].lower(), google_subject=claims["sub"], password_hash=None,
            ).on_conflict_do_nothing().returning(User.id))
        user = db.scalar(select(User).where(User.google_subject == claims["sub"]))
    if created_user_id is not None and get_settings().seed_new_user_examples:
        from app.demo.onboarding import provision_examples
        provision_examples(db, user)
    db.commit()  # consume even a collision challenge; do not let it be replayed
    if user is None:
        raise HTTPException(409, "An account already uses this email. Sign in with your existing method.")
    return user
