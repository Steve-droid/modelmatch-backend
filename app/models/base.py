"""Declarative base for all ORM models.

The DDL itself is owned by the Alembic migrations (S2) — these models must MATCH
that schema, not generate it (we never call `create_all`). `Base.metadata` is fed
to Alembic's env.py as `target_metadata` so future migrations can `--autogenerate`
against the models, and the S3 parity test asserts the two never drift.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
