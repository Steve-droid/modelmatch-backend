"""SELECT-only safety gate for LLM-generated catalog SQL (S14b).

The chat's LLM effectively injects SQL into our database, so every generated query
is validated HERE before it reaches Postgres — the app-level half of the safety
story (the read-only `chat_catalog`-only role is the DB-level half, see the migration).

We parse with sqlglot rather than string-matching, because a parser is not fooled by
comments, string literals, or a write hidden inside a CTE
(`WITH x AS (DELETE ... RETURNING *) SELECT ...`, which a naive `startswith("select")`
would wave through).

Policy: exactly one statement, whose root is a SELECT, with no data-modifying or DDL
node anywhere in the tree, and no `SELECT ... INTO`. A `LIMIT` is injected when the
query has none, to bound result size. (Mirrors the ABC SQL-RAG gate.)
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

DEFAULT_MAX_LIMIT = 200

# Any of these anywhere in the tree means the query is not read-only — this is what
# catches data-modifying CTEs that still have a SELECT at the root.
_FORBIDDEN: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Command,
    exp.Grant,
    exp.Copy,
)


@dataclass(frozen=True)
class GateResult:
    ok: bool
    sql: str | None = None  # the validated, LIMIT-bounded SQL (when ok)
    reason: str | None = None  # why it was rejected (when not ok)


def check_sql(sql: str, *, max_limit: int = DEFAULT_MAX_LIMIT) -> GateResult:
    """Validate that `sql` is a safe, read-only SELECT and return a bounded form."""
    text = (sql or "").strip()
    if not text:
        return GateResult(ok=False, reason="Empty query.")

    try:
        statements = [s for s in sqlglot.parse(text, dialect="postgres") if s is not None]
    except SqlglotError:
        return GateResult(ok=False, reason="Could not parse the SQL.")

    if len(statements) != 1:
        return GateResult(ok=False, reason="Only a single statement is allowed.")

    statement = statements[0]
    if not isinstance(statement, exp.Select):
        return GateResult(ok=False, reason="Only SELECT queries are allowed.")

    if statement.args.get("into") is not None:
        return GateResult(ok=False, reason="SELECT ... INTO is not allowed.")

    if any(statement.find_all(*_FORBIDDEN)):
        return GateResult(ok=False, reason="Only read-only SELECT queries are allowed.")

    if statement.args.get("limit") is None:
        statement = statement.limit(max_limit)

    return GateResult(ok=True, sql=statement.sql(dialect="postgres"))
