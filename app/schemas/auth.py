"""Auth contract schemas (used in S4). password_hash is NEVER exposed."""

from pydantic import EmailStr, Field

from app.schemas.base import CamelModel


class UserCreate(CamelModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class UserOut(CamelModel):
    id: int
    email: EmailStr
    chat_enabled: bool = False

    @classmethod
    def from_user(cls, user):
        from app.config import get_settings
        return cls(id=user.id, email=user.email,
                   chat_enabled=bool(user.is_operator and get_settings().chat_enabled))


class LoginRequest(CamelModel):
    email: EmailStr
    password: str = Field(max_length=1024)


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
