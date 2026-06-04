"""Shared test fixtures.

`db_session` gives an ORM Session bound to a freshly-migrated throwaway database
on the compose Postgres — so DB-backed tests never touch dev data and skip cleanly
when no database is reachable.
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = "modelmatch_orm_test"


@pytest.fixture
def db_session():
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
        with Session(engine) as session:
            yield session
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
