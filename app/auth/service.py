"""User persistence for auth: lookup, register, authenticate.

Thin layer over the ORM so the routes stay declarative. Authentication returns
None on any failure (unknown email or wrong password) — the route maps that to a
single 401 so the response never reveals which emails exist.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.security import hash_password, verify_password
from app.models import User


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == email))


def register_user(db: Session, email: str, password: str) -> User:
    user = User(email=email, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: Session, email: str, password: str) -> User | None:
    user = get_user_by_email(db, email)
    if user is None or not verify_password(user.password_hash, password):
        return None
    return user
