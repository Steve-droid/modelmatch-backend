"""Password hashing (argon2) and JWT access tokens (HS256, pyjwt).

Two primitives, no I/O: hashing/verification and token mint/decode. The secret +
TTL come from config (env-injected in deploy). Plaintext passwords live only on
the stack here and are never stored or logged — only the argon2 hash is persisted.
"""

from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

from app.config import get_settings

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Return an argon2 hash (includes algorithm, salt, and parameters)."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """True iff the password matches the stored hash (never raises on mismatch)."""
    try:
        return _hasher.verify(password_hash, password)
    except Argon2Error:
        return False


def create_access_token(subject: str) -> str:
    """Mint a short-lived HS256 token whose `sub` claim is the user id."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expires_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """Decode + verify signature and expiry. Raises jwt.PyJWTError on any failure."""
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
