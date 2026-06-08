"""Read-only SQL execution for the chat pipeline (S14b).

Catalog queries run through a dedicated engine that connects as the restricted
`chat_readonly_db_user` role (see the chat-role migration). That role is the
database-level half of the safety story: even if the app-level SELECT gate is
bypassed, the role can only SELECT the curated `chat_catalog` view — it cannot write,
and cannot read the base tables (`user` credentials, `jenkins_connection` secret refs,
per-tenant `ci_run` rows).

The SQL is run via `sqlalchemy.text()` and never string-built from the user's
question (the question is an LLM input, not concatenated into SQL). Any bound
parameters are passed separately and substituted by the driver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.config import get_settings

_readonly_engine: Engine | None = None


def get_readonly_engine() -> Engine:
    """The shared read-only chat engine, created on first use (so importing this
    module never requires the role to exist — e.g. in unrelated tests)."""
    global _readonly_engine
    if _readonly_engine is None:
        _readonly_engine = create_engine(
            get_settings().chat_database_url, pool_pre_ping=True
        )
    return _readonly_engine


@dataclass(frozen=True)
class QueryResult:
    """What the chat needs from a query: the rows plus enough to build the trace."""

    columns: list[str]
    rows: list[tuple] = field(default_factory=list)
    row_count: int = 0


def run_readonly(
    sql: str,
    *,
    engine: Engine | None = None,
    params: dict[str, Any] | None = None,
) -> QueryResult:
    """Execute `sql` on the read-only engine and return rows + columns + count.

    Raises the underlying SQLAlchemy error on failure (permission denied, bad SQL,
    etc.) — callers handle/retry. `engine` is injectable for tests; `params` are bound
    by the driver, never interpolated."""
    eng = engine or get_readonly_engine()
    with eng.connect() as conn:
        result = conn.execute(text(sql), params or {})
        columns = list(result.keys())
        rows = [tuple(row) for row in result.fetchall()]
    return QueryResult(columns=columns, rows=rows, row_count=len(rows))
