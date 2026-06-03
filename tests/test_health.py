"""S1 contract tests: liveness/readiness split + env-driven config.

These define what the scaffold promises:
- /healthz is liveness — it must NOT depend on the database.
- /readyz is readiness — it reflects whether the DB is reachable (503 if not).
- config is env-driven — CORS origins arrive as a comma-separated env string.
"""

import pytest
from fastapi.testclient import TestClient

from app import __version__
from app.config import Settings
from app.main import app

client = TestClient(app)


def test_healthz_is_ok_even_when_db_is_down(monkeypatch):
    """Liveness must stay up regardless of the database (no dependency checks)."""
    monkeypatch.setattr("app.main.check_db", lambda: False)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_ready_when_db_reachable(monkeypatch):
    monkeypatch.setattr("app.main.check_db", lambda: True)
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "db": "ok"}


def test_readyz_503_when_db_unreachable(monkeypatch):
    monkeypatch.setattr("app.main.check_db", lambda: False)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "not_ready", "db": "unavailable"}


def test_root_reports_version():
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["version"] == __version__


def test_cors_origins_parse_from_csv_env(monkeypatch):
    """Env-driven, no hardcoding: CORS_ALLOW_ORIGINS as CSV -> list[str]."""
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "http://a.test, http://b.test")
    settings = Settings(_env_file=None)
    assert settings.cors_allow_origins == ["http://a.test", "http://b.test"]
