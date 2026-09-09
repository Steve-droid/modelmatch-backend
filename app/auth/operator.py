"""Explicit offline operator grant/revocation. No public role-management API."""
import argparse

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import User


def set_operator(db: Session, user_id: int, expected_email: str, enabled: bool) -> None:
    db.execute(text("SELECT pg_advisory_xact_lock(384701)"))
    user = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None or user.email != expected_email:
        raise ValueError("Account identity did not match; no permission changed")
    # This single-operator demo must not accidentally retain a second privileged user.
    if enabled and db.scalar(select(User.id).where(User.is_operator.is_(True), User.id != user_id)):
        raise ValueError("Revoke the existing operator before granting another account")
    user.is_operator = enabled
    db.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--expected-email", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--grant", action="store_true")
    group.add_argument("--revoke", action="store_true")
    args = parser.parse_args()
    from app.db import SessionLocal
    with SessionLocal() as db:
        set_operator(db, args.user_id, args.expected_email, args.grant)
    print(f"Operator permission {'granted' if args.grant else 'revoked'} for user ID {args.user_id}")


if __name__ == "__main__":
    main()
