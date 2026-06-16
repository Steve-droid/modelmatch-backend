"""S2 contract test: the initial migration round-trips, and the schema carries
NO embedding/vector columns (the recommender is deterministic; pgvector is dropped).

This is an integration test: it needs the compose Postgres (`docker compose up -d db`).
It runs on a *throwaway* database so it never touches dev data, and skips cleanly
if no database is reachable.

What it pins:
- `upgrade head` builds every §5 entity (incl. source_document / chat_message /
  retrieval_trace — the chat #4 + ingestion #3 tables the backlog calls out).
- NO column anywhere is an embedding/vector, and the pgvector type is absent.
- `downgrade base` fully reverses: no tables AND no leftover enum types.
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = "modelmatch_migration_test"

# Every table the initial migration must create (architecture.md §5 + the
# llm_usage tally added in S2). The backlog names the starred ones explicitly.
EXPECTED_TABLES = {
    "user",
    "model",
    "agent_runtime_config",
    "harness",
    "benchmark",
    "source_document",  # ingestion #3 input (content_hash = idempotency key)
    "benchmark_result",
    "requirements_profile",
    "recommendation_option",
    "recommendation_evidence",  # FK-enforced provenance join table
    "project",
    "jenkins_connection",
    "ci_run",
    "ci_finding",
    "finding_feedback",
    "chat_message",  # grounded chat #4
    "retrieval_trace",  # grounded chat #4
    "llm_call",
    "proactive_alert",
    "llm_usage",
}


def _admin_engine():
    """Engine on the default DB, AUTOCOMMIT — used to create/drop the throwaway DB."""
    admin_url = make_url(get_settings().database_url)
    return create_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)


@pytest.fixture
def migration_db():
    """Create a throwaway database, yield an Alembic Config pointed at it, drop it."""
    try:
        admin = _admin_engine()
        with admin.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("Postgres not reachable — run `docker compose up -d db`")

    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))

    test_url = make_url(get_settings().database_url).set(database=TEST_DB)

    # Point app.config (and therefore Alembic's env.py) at the throwaway DB.
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = test_url.render_as_string(hide_password=False)
    get_settings.cache_clear()

    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))

    try:
        yield cfg, test_url
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev
        get_settings.cache_clear()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
        admin.dispose()


def _enum_type_names(engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT typname FROM pg_type WHERE typtype = 'e'"))
        return {r[0] for r in rows}


def test_upgrade_builds_all_entities_with_no_embedding_columns(migration_db):
    cfg, test_url = migration_db
    from alembic import command

    command.upgrade(cfg, "head")

    engine = create_engine(test_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())

        # Every expected entity exists (alembic_version is bookkeeping, ignored).
        missing = EXPECTED_TABLES - tables
        assert not missing, f"migration did not create: {sorted(missing)}"

        # NO embedding/vector columns anywhere — the deterministic-recommender guarantee.
        for table in EXPECTED_TABLES:
            for col in inspector.get_columns(table):
                name = col["name"].lower()
                assert "embedding" not in name and "vector" not in name, (
                    f"unexpected embedding column {table}.{col['name']}"
                )
                assert "vector" not in str(col["type"]).lower(), (
                    f"unexpected vector type on {table}.{col['name']}"
                )

        # pgvector extension type must not be installed.
        with engine.connect() as conn:
            has_vector = conn.execute(
                text("SELECT 1 FROM pg_type WHERE typname = 'vector'")
            ).first()
        assert has_vector is None, "pgvector type present — embeddings are dropped"
    finally:
        engine.dispose()


def test_downgrade_reverses_tables_and_enum_types(migration_db):
    cfg, test_url = migration_db
    from alembic import command

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    engine = create_engine(test_url)
    try:
        inspector = inspect(engine)
        leftover_tables = set(inspector.get_table_names()) - {"alembic_version"}
        assert not leftover_tables, f"downgrade left tables: {sorted(leftover_tables)}"

        leftover_enums = _enum_type_names(engine)
        assert not leftover_enums, f"downgrade left enum types: {sorted(leftover_enums)}"
    finally:
        engine.dispose()
