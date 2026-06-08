"""chat read-only role + curated chat_catalog view (S14b grounded chat #4)

The grounded Q&A chat lets the LLM generate SQL over the catalog. That SQL is
UNTRUSTED, so it is fenced by two layers:

1. an app-level SELECT-only gate (sqlglot AST — `app/chat/gate.py`), and
2. this migration's DATABASE-level boundary: a curated `chat_catalog` VIEW
   (safe, denormalized catalog columns only) plus a restricted `modelmatch_chat_ro`
   login role granted SELECT on ONLY that view.

Because a non-materialized view in Postgres accesses its underlying tables with the
VIEW OWNER's privileges (the migration runs as the DB owner), the read-only role can
read the catalog THROUGH the view while having NO grant on the base tables. So even
if the app gate were bypassed, `SELECT password_hash FROM "user"` or a read of the
`jenkins_connection` secret refs fails at the database — and every write fails too.

Savings questions never touch this path: savings are retrieved deterministically by
the trusted S14 engine on the app's normal connection. Only LLM-generated catalog SQL
runs as this role.

The role password is env-driven (`CHAT_READONLY_DB_PASSWORD`), never hardcoded. Role
creation is idempotent (roles are cluster-global; this migration may run against many
databases). In production this role/grant would live in IaC + a secrets manager; it is
a migration here for single-command setup, matching the rest of the project.

Revision ID: a1b2c3d4e5f6
Revises: f6a7b8c9d0e1
Create Date: 2026-06-08 12:00:00.000000
"""
import re
from typing import Sequence, Union

from alembic import op

from app.config import get_settings


def _safe_identifier(name: str) -> str:
    """Re-validate the role name before interpolating it into role DDL (config already
    validates it; this is defense-in-depth so the migration is safe on its own)."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or len(name) > 63:
        raise ValueError(f"unsafe chat role identifier: {name!r}")
    return name


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The curated, denormalized catalog view the chat may read. Mirrors
# `catalog.service.list_catalog`'s shape — model/benchmark/harness names + the metric
# figures + split prices. Deliberately excludes every secret/identity/tenant column
# (user.password_hash, jenkins_connection.*_ref, project, ci_run, llm_call, ...).
_CREATE_VIEW = """
CREATE VIEW chat_catalog AS
SELECT
    br.id                   AS id,
    m.name                  AS model,
    m.vendor                AS vendor,
    b.name                  AS benchmark,
    h.name                  AS harness,
    h.vendor                AS harness_vendor,
    br.task_type            AS task_type,
    br.metric               AS metric,
    br.score                AS score,
    br.cost_per_mtok        AS cost_per_mtok,
    m.input_price_per_mtok  AS input_price_per_mtok,
    m.output_price_per_mtok AS output_price_per_mtok,
    br.context_window       AS context_window,
    br.source               AS source,
    br.measured_at          AS measured_at
FROM benchmark_result br
JOIN model m      ON m.id = br.model_id
JOIN benchmark b  ON b.id = br.benchmark_id
LEFT JOIN harness h ON h.id = br.harness_id
"""

ROLE = "modelmatch_chat_ro"


def upgrade() -> None:
    settings = get_settings()
    role = _safe_identifier(settings.chat_readonly_db_user)
    pw = settings.chat_readonly_db_password.replace("'", "''")

    op.execute(_CREATE_VIEW)

    # Idempotent: create the login role only if absent, then sync its password to
    # config (roles are cluster-global, so this may run after the role exists).
    op.execute(
        f"DO $$ BEGIN "
        f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN "
        f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{pw}'; "
        f"END IF; END $$;"
    )
    op.execute(f'ALTER ROLE "{role}" WITH LOGIN PASSWORD \'{pw}\';')
    # SELECT on ONLY the curated view — no grant on any base table. USAGE on the schema
    # is needed to resolve the view name. CONNECT is inherited from PUBLIC's default.
    op.execute(f'GRANT USAGE ON SCHEMA public TO "{role}";')
    op.execute(f'GRANT SELECT ON chat_catalog TO "{role}";')


def downgrade() -> None:
    settings = get_settings()
    role = _safe_identifier(settings.chat_readonly_db_user)
    # Revoke this database's grants and drop the curated view. We deliberately do NOT
    # DROP ROLE: roles are CLUSTER-GLOBAL, so the same role may be in use by other
    # databases in this cluster, and DROP ROLE fails if it still owns objects or holds
    # grants elsewhere. Revoking its access here is the correct, reversible boundary;
    # decommissioning the role itself is an operator/IaC action, not a per-DB migration.
    op.execute(f'REVOKE SELECT ON chat_catalog FROM "{role}";')
    op.execute(f'REVOKE USAGE ON SCHEMA public FROM "{role}";')
    op.execute("DROP VIEW IF EXISTS chat_catalog;")
