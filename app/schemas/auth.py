"""Auth contract schemas (used in S4). password_hash is NEVER exposed."""

from pydantic import EmailStr

from app.schemas.base import CamelModel


class UserCreate(CamelModel):
    email: EmailStr
    password: str  # plaintext in transit only; hashed (argon2) before storage in S4


class UserOut(CamelModel):
    id: int
    email: EmailStr
