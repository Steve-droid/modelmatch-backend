"""Auth contract schemas (used in S4). password_hash is NEVER exposed."""

from pydantic import EmailStr, Field

from app.schemas.base import CamelModel


class UserCreate(CamelModel):
    email: EmailStr
    password: str  # plaintext in transit only; hashed (argon2) before storage in S4


class UserOut(CamelModel):
    id: int
    email: EmailStr


class LoginRequest(CamelModel):
    email: EmailStr
    password: str


class TokenOut(CamelModel):
    access_token: str
    token_type: str = "bearer"


class GoogleChallengeOut(CamelModel):
    client_id: str
    nonce: str
    challenge: str


class GoogleLoginRequest(CamelModel):
    # Bounds prevent oversized token parsing / validation responses.
    credential: str = Field(min_length=1, max_length=16384)
    challenge: str = Field(min_length=1, max_length=2048)
