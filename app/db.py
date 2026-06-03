"""Database engine/session wiring (SQLAlchemy 2.0, sync engine + psycopg).

Migrations run as a separate step (Alembic, S2) — never on startup. This module
only sets up the engine and a readiness probe.
"""

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import get_settings

engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def check_db() -> bool:
    """Readiness check: can we reach the database?"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
