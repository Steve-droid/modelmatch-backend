"""Database engine/session wiring (SQLAlchemy 2.0, sync engine + psycopg).

Migrations run as a separate step (Alembic, S2) — never on startup. This module
only sets up the engine and a readiness probe.
"""

from time import perf_counter

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.observability.metrics import observe_db_query

engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


# --- DB query timing (P20) ----------------------------------------------------------
# Time every statement at the DBAPI-cursor boundary and record it under its SQL verb.
# Listening on the Engine CLASS (not just `engine`) instruments every engine the process
# creates — in production that's the one app engine above; the breadth is harmless since
# the histogram is process-global. The statement TEXT is never read into a metric — only
# the leading keyword (a bounded label), so query values / PII never reach Prometheus.
def _sql_operation(statement: str) -> str:
    verb = statement.lstrip().split(" ", 1)[0].upper() if statement else ""
    return verb if verb in {"SELECT", "INSERT", "UPDATE", "DELETE"} else "OTHER"


@event.listens_for(Engine, "before_cursor_execute")
def _db_query_start(conn: Connection, cursor, statement, parameters, context, executemany):
    conn.info["_query_start"] = perf_counter()


@event.listens_for(Engine, "after_cursor_execute")
def _db_query_end(conn: Connection, cursor, statement, parameters, context, executemany):
    start = conn.info.pop("_query_start", None)
    if start is not None:
        observe_db_query(_sql_operation(statement), perf_counter() - start)


def check_db() -> bool:
    """Readiness check: can we reach the database?"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
