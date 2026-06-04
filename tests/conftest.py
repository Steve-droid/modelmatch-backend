"""Shared test fixtures.

Everything DB-backed runs against a freshly-migrated *throwaway* database on the
compose Postgres, so tests never touch dev data and skip cleanly with no DB.

- migrated_engine: the throwaway DB + `upgrade head`, as an Engine (one per test).
- db_session: an ORM Session on it.
- client: a TestClient with `get_db` overridden onto the SAME engine — so a request
  through `client` and a query through `db_session` see the same database.
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

# JWT_SECRET is required (no default) — provide an explicit test value before any
# test imports app.main / builds Settings. setdefault respects a real env if set.
os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-not-for-production-0123456789")

from app.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = "modelmatch_orm_test"


@pytest.fixture
def migrated_engine():
    admin_url = make_url(get_settings().database_url)
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        with admin.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        admin.dispose()
        pytest.skip("Postgres not reachable — run `docker compose up -d db`")

    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))

    test_url = make_url(get_settings().database_url).set(database=TEST_DB)
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = test_url.render_as_string(hide_password=False)
    get_settings.cache_clear()

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")

    engine = create_engine(test_url)
    try:
        yield engine
    finally:
        engine.dispose()
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev
        get_settings.cache_clear()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def db_session(migrated_engine):
    with Session(migrated_engine) as session:
        yield session


@pytest.fixture
def client(migrated_engine):
    from fastapi.testclient import TestClient

    from app.auth.deps import get_db
    from app.main import app

    TestSession = sessionmaker(bind=migrated_engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
