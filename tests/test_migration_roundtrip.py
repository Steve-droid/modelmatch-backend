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
    "google_login_nonce",
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

        # Trusted CI runtime config is bootstrapped by migrations, not only by the
        # offline test seed path. The three demo-supported runtime mappings must be
        # present immediately after `upgrade head`.
        with engine.connect() as conn:
            runtime_rows = conn.execute(
                text(
                    """
                    SELECT m.name, m.vendor, arc.provider, arc.provider_model_id,
                           arc.auth_mode, arc.credential_env_var, arc.enabled
                    FROM agent_runtime_config arc
                    JOIN model m ON m.id = arc.model_id
                    ORDER BY m.vendor, m.name
                    """
                )
            ).all()
        assert len(runtime_rows) == 3
        assert runtime_rows == [
            (
                "Nova 2 Lite",
                "Amazon",
                "bedrock",
                "global.amazon.nova-2-lite-v1:0",
                "aws_iam",
                None,
                True,
            ),
            (
                "Claude Haiku 4.5",
                "Anthropic",
                "anthropic",
                "claude-haiku-4-5",
                "api_key",
                "ANTHROPIC_API_KEY",
                True,
            ),
            (
                "Gemini 2.5 Flash",
                "Google",
                "gemini",
                "gemini-2.5-flash",
                "api_key",
                "GOOGLE_API_KEY",
                True,
            ),
        ]
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


def test_two_tasks_migration_backfills_task_and_cwe_and_reverses(migration_db):
    """E20 (d1e2f3a4b5c6): upgrading a database that holds pre-E20 rows sets each
    project's task from its recommendation, turns `code_review` runs into `ci_review`,
    lifts the CWE prefix out of seeded security findings, and downgrades cleanly."""
    from alembic import command

    cfg, test_url = migration_db
    command.upgrade(cfg, "c9d0e1f2a3b4")  # the revision before P38e

    engine = create_engine(test_url)
    try:
        with engine.begin() as conn:
            # ids from 900 up: the runtime-config seed migration already owns low model ids
            conn.execute(text("INSERT INTO \"user\" (id, email, password_hash) VALUES (1, 'u@x', 'h')"))
            conn.execute(text("INSERT INTO model (id, name, vendor) VALUES (900, 'M', 'V')"))
            conn.execute(text(
                "INSERT INTO requirements_profile (id, user_id, task_types) VALUES "
                "(1, 1, ARRAY['ci_review']), (2, 1, ARRAY['security_analysis']), (3, 1, NULL)"
            ))
            conn.execute(text(
                "INSERT INTO recommendation_option (id, profile_id, model_id) VALUES "
                "(1, 1, 900), (2, 2, 900), (3, 3, 900)"
            ))
            conn.execute(text(
                "INSERT INTO project (id, user_id, name, selected_option_id, baseline_model_id) VALUES "
                "(1, 1, 'review', 1, 900), (2, 1, 'sec', 2, 900), (3, 1, 'legacy', 3, 900), (4, 1, 'none', NULL, 900)"
            ))
            conn.execute(text(
                "INSERT INTO ci_run (id, project_id, jenkins_build_id, task) VALUES "
                "(1, 1, 'a', 'code_review'), (2, 2, 'b', 'security_analysis')"
            ))
            conn.execute(text(
                "INSERT INTO ci_finding (id, ci_run_id, severity, category, file, line, message) VALUES "
                "(1, 2, 'critical', 'security', 'a.py', 1, 'CWE-89: SQL injection — request parameter concatenated'), "
                "(2, 2, 'low', 'security', 'b.py', 2, 'CWE-327: weak hash (MD5) used for a security decision'), "
                "(3, 1, 'low', 'style', 'c.py', 3, 'Unused import')"
            ))

        command.upgrade(cfg, "head")

        with engine.connect() as conn:
            tasks = dict(conn.execute(text("SELECT id, task_type FROM project ORDER BY id")).all())
            assert tasks == {1: "ci_review", 2: "security_analysis", 3: "ci_review", 4: "ci_review"}
            assert conn.execute(text("SELECT review_preferences FROM project WHERE id = 1")).scalar() is None
            run_tasks = dict(conn.execute(text("SELECT id, task FROM ci_run ORDER BY id")).all())
            assert run_tasks == {1: "ci_review", 2: "security_analysis"}
            cwes = dict(conn.execute(text("SELECT id, cwe FROM ci_finding ORDER BY id")).all())
            assert cwes == {
                1: "CWE-89: SQL injection",
                2: "CWE-327: weak hash (MD5) used for a security decision",
                3: None,
            }
            assert conn.execute(text("SELECT cache_read_tokens FROM ci_run WHERE id = 1")).scalar() is None
            # the new default applies to a fresh run
            conn.execute(text("INSERT INTO ci_run (id, project_id, jenkins_build_id) VALUES (3, 1, 'c')"))
            assert conn.execute(text("SELECT task FROM ci_run WHERE jenkins_build_id = 'c'")).scalar() == "ci_review"
            conn.commit()

        command.downgrade(cfg, "c9d0e1f2a3b4")
        inspector = inspect(engine)
        assert "task_type" not in {c["name"] for c in inspector.get_columns("project")}
        assert "review_preferences" not in {c["name"] for c in inspector.get_columns("project")}
        assert "cwe" not in {c["name"] for c in inspector.get_columns("ci_finding")}
        assert "cache_read_tokens" not in {c["name"] for c in inspector.get_columns("ci_run")}
        with engine.connect() as conn:
            run_tasks = dict(conn.execute(text("SELECT id, task FROM ci_run ORDER BY id")).all())
            assert run_tasks[1] == "code_review" and run_tasks[2] == "security_analysis"
        command.upgrade(cfg, "head")  # up / down / up
    finally:
        engine.dispose()


def test_google_migration_preserves_users_and_projects(migration_db):
    from alembic import command
    from app.auth.security import verify_password

    cfg, test_url = migration_db
    command.upgrade(cfg, "d1e2f3a4b5c6")
    engine = create_engine(test_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO \"user\" (id,email,password_hash) VALUES (900,'legacy@example.com','old-hash')"))
            conn.execute(text("INSERT INTO project (id,user_id,name) VALUES (900,900,'preserved-project')"))
        command.upgrade(cfg, "e2f3a4b5c6d7")
        with engine.begin() as conn:
            assert conn.execute(text('SELECT password_hash FROM "user" WHERE id=900')).scalar() == "old-hash"
            conn.execute(text("INSERT INTO \"user\" (id,email,google_subject) VALUES (901,'google@example.com','google-sub')"))
            conn.execute(text("INSERT INTO project (id,user_id,name) VALUES (901,901,'google-project')"))
        command.downgrade(cfg, "d1e2f3a4b5c6")
        with engine.connect() as conn:
            hashes = dict(conn.execute(text('SELECT id,password_hash FROM "user" ORDER BY id')).all())
            assert hashes == {900: "old-hash", 901: "$argon2id$disabled"}
            assert not verify_password(hashes[901], "!")
            assert conn.execute(text("SELECT count(*) FROM project WHERE id IN (900,901)")).scalar() == 2
        command.upgrade(cfg, "head")
    finally:
        engine.dispose()
