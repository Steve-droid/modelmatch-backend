"""Short Postgres transactions for account capacity and aggregate auth rate limits.

All hosts/workers share one bounded token bucket; it stores no IPs or identities.
Ingress adds per-IP fairness. Neither mechanism claims distributed-DDoS protection.
"""
import math

from fastapi import Depends, HTTPException
from prometheus_client import Counter
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.deps import get_db
from app.config import get_settings
from app.models import User

REJECTIONS = Counter("modelmatch_admission_rejections_total",
                     "Admission rejections by bounded reason.", ["reason"])


def lock_registration(db: Session) -> None:
    # Transaction-scoped, shared by EVERY account writer. READ COMMITTED sees the
    # preceding transaction's committed account when the lock becomes available.
    db.execute(text("SET LOCAL lock_timeout = '5s'"))
    db.execute(text("SELECT pg_advisory_xact_lock(384700)"))


def require_capacity(db: Session) -> None:
    """Must be called under lock_registration, immediately before inserting."""
    if db.scalar(select(func.count()).select_from(User)) >= get_settings().max_registered_users:
        REJECTIONS.labels("capacity").inc()
        raise HTTPException(409, detail={
            "code": "registration_capacity_reached",
            "message": "Registration is currently full. Existing users can still sign in.",
        })


def limit_auth_requests(db: Session = Depends(get_db)) -> None:
    settings = get_settings()
    if not settings.auth_rate_limit_enabled:
        return
    # Atomic token-bucket UPSERT; a rejected attempt does not move the refill time.
    # Commit before password hashing / Google's network verification / registration.
    try:
        db.execute(text("SET LOCAL lock_timeout = '2s'"))
        allowed = db.scalar(text("""
            INSERT INTO auth_rate_bucket AS b (id, tokens, updated_at)
            VALUES (1, :burst - 1, clock_timestamp())
            ON CONFLICT (id) DO UPDATE
            SET tokens = LEAST(:burst, b.tokens + GREATEST(0, EXTRACT(EPOCH FROM (clock_timestamp() - b.updated_at))) * :rate) - 1, updated_at = clock_timestamp()
            WHERE LEAST(:burst, b.tokens + GREATEST(0, EXTRACT(EPOCH FROM (clock_timestamp() - b.updated_at))) * :rate) >= 1
            RETURNING id
        """), {"burst": settings.auth_burst, "rate": settings.auth_requests_per_minute / 60})
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        REJECTIONS.labels("unavailable").inc()
        raise HTTPException(503, "Sign-in is temporarily unavailable") from None
    if allowed is None:
        REJECTIONS.labels("rate").inc()
        raise HTTPException(429, "Too many sign-in attempts. Please try again shortly.",
                            headers={"Retry-After": str(max(1, math.ceil(60 / settings.auth_requests_per_minute)))})
