"""User persistence for auth: lookup, register, authenticate.

Thin layer over the ORM so the routes stay declarative. Authentication returns
None on any failure (unknown email or wrong password) — the route maps that to a
single 401 so the response never reveals which emails exist.
"""

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.security import hash_password, verify_password
from app.auth.admission import lock_registration, require_capacity
from app.models import User
from app.config import get_settings


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == email))


def register_user(db: Session, email: str, password: str, *, with_examples: bool = True) -> User:
    # Hash before acquiring the short registration lock; ingress + auth bucket bound work.
    password_hash = hash_password(password)
    try:
        lock_registration(db)
        if db.scalar(select(User.id).where(func.lower(User.email) == email.lower())) is not None:
            raise HTTPException(409, "Email already registered")
        require_capacity(db)
        user = User(email=email, password_hash=password_hash)
        db.add(user)
        db.flush()
        if with_examples and get_settings().seed_new_user_examples:
            from app.demo.onboarding import provision_examples
            provision_examples(db, user)
        db.commit()
        db.refresh(user)
        return user
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Email already registered") from None
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(503, "Registration is temporarily unavailable") from None


def authenticate_user(db: Session, email: str, password: str) -> User | None:
    user = get_user_by_email(db, email)
    if user is None or user.password_hash is None or not verify_password(user.password_hash, password):
        return None
    return user
