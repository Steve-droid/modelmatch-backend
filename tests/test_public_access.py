"""Public-demo boundaries against real PostgreSQL; no provider calls."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.auth import google, service
from app.auth.admission import limit_auth_requests
from app.auth.operator import set_operator
from app.config import get_settings
from app.models import User, ChatMessage, LlmUsage
from tests.test_google_auth import google_env, sign_in


@pytest.fixture
def admission_env(monkeypatch):
    monkeypatch.setenv("MAX_REGISTERED_USERS", "3")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def register(client, email="visitor@example.com", **extra):
    return client.post("/auth/register", json={"email": email, "password": "test-password", **extra})


def headers(client, email="visitor@example.com"):
    token = client.post("/auth/login", json={"email": email, "password": "test-password"}).json()["accessToken"]
    return {"Authorization": f"Bearer {token}"}


def test_cap_preserves_logins_and_never_grants_browser_permissions(client, db_session, admission_env):
    for n in range(3):
        result = register(client, f"v{n}@example.com", isOperator=True, chatEnabled=True)
        assert result.status_code == 201
        assert result.json()["chatEnabled"] is False
    rejected = register(client)
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "registration_capacity_reached"
    assert client.get("/auth/me", headers=headers(client, "v0@example.com")).status_code == 200
    assert db_session.scalar(select(func.count()).select_from(User)) == 3
    assert not db_session.scalar(select(User.id).where(User.is_operator.is_(True)))
    admission_env.setenv("MAX_REGISTERED_USERS", "0")
    get_settings.cache_clear()
    assert client.get("/auth/me", headers=headers(client, "v0@example.com")).status_code == 200
    assert register(client).status_code == 409


def test_google_cap_returning_login_and_shared_password_limit(client, google_env, admission_env):
    assert register(client).status_code == 201
    assert sign_in(client, google_env)[0].status_code == 200
    assert register(client, "last@example.com").status_code == 201
    assert sign_in(client, google_env, sub="new-sub", email="full@example.com")[0].status_code == 409
    assert sign_in(client, google_env)[0].status_code == 200
    assert client.get("/auth/me", headers=headers(client)).status_code == 200


def test_concurrent_mixed_signup_last_slot(migrated_engine, admission_env):
    admission_env.setattr(service, "hash_password", lambda _: "test-hash")
    # Each thread has a fresh signed challenge; only identity verification is mocked.
    admission_env.setattr(google, "verify_credential", lambda credential, nonce: {
        "sub": credential, "email": f"{credential}@example.com",
    })
    with Session(migrated_engine) as db:
        db.add_all([User(email=f"old{n}@example.com", password_hash="hash") for n in range(2)])
        db.commit()
    barrier = Barrier(8)
    def signup(n):
        challenge = google.create_challenge().challenge
        barrier.wait()
        with Session(migrated_engine) as db:
            try:
                if n % 2:
                    google.authenticate_google(db, f"g{n}", challenge)
                else:
                    service.register_user(db, f"p{n}@example.com", "pw")
                return 201
            except HTTPException as exc:
                return exc.status_code
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(signup, range(8)))
    assert results.count(201) == 1
    assert results.count(409) == 7
    with Session(migrated_engine) as db:
        assert db.scalar(select(func.count()).select_from(User)) == 3


def test_duplicate_and_rollback_do_not_consume_slots(migrated_engine, admission_env):
    admission_env.setattr(service, "hash_password", lambda _: "test-hash")
    with Session(migrated_engine) as db:
        service.register_user(db, "same@example.com", "pw")
        with pytest.raises(HTTPException) as duplicate:
            service.register_user(db, "SAME@example.com", "pw")
        assert duplicate.value.status_code == 409
        db.add(User(email="rollback@example.com", password_hash="hash"))
        db.flush()
        db.rollback()
        service.register_user(db, "next@example.com", "pw")
        service.register_user(db, "last@example.com", "pw")
        assert db.scalar(select(func.count()).select_from(User)) == 3


def test_concurrent_same_google_identity_at_capacity(migrated_engine, admission_env):
    admission_env.setenv("MAX_REGISTERED_USERS", "1")
    get_settings.cache_clear()
    admission_env.setattr(google, "verify_credential", lambda credential, nonce: {
        "sub": "same-subject", "email": "same@example.com",
    })
    barrier = Barrier(2)
    def signup(_):
        challenge = google.create_challenge().challenge
        barrier.wait()
        with Session(migrated_engine) as db:
            return google.authenticate_google(db, "fixture", challenge).id
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(signup, range(2)))
    assert ids[0] == ids[1]
    with Session(migrated_engine) as db:
        assert db.scalar(select(func.count()).select_from(User)) == 1


def test_upgrade_preserves_existing_users_and_grants_nobody(migrated_engine):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    command.downgrade(config, "e2f3a4b5c6d7")
    with migrated_engine.begin() as conn:
        uid = conn.scalar(text('INSERT INTO "user" (email,password_hash) VALUES (:email,:hash) RETURNING id'),
                          {"email": "existing@example.com", "hash": "existing-password-hash"})
    command.upgrade(config, "head")
    with Session(migrated_engine) as db:
        user = db.get(User, uid)
        assert user.password_hash == "existing-password-hash"
        assert user.is_operator is False


@pytest.mark.parametrize("path,method,payload", [
    ("/projects/1/chat", "get", None),
    ("/projects/1/chat", "post", {"question": "How much have I saved?"}),
    ("/benchmarks/ingest", "post", {"sourceText": "paid source"}),
    ("/benchmarks", "post", {}),
])
def test_paid_and_catalog_routes_deny_before_work(client, db_session, path, method, payload):
    from app.api.chat import get_chat_engine, get_chat_llm_client
    from app.api.benchmarks import build_ingest_client
    from app.main import app
    forbidden = Mock(side_effect=AssertionError("Provider or retrieval dependency constructed"))
    for dependency in (get_chat_engine, get_chat_llm_client, build_ingest_client):
        app.dependency_overrides[dependency] = forbidden
    kwargs = {"json": payload} if payload is not None else {}
    assert getattr(client, method)(path, **kwargs).status_code == 401
    register(client)
    auth = headers(client)
    assert getattr(client, method)(path, headers=auth, **kwargs).status_code == 403
    forbidden.assert_not_called()
    assert db_session.scalar(select(func.count()).select_from(ChatMessage)) == 0
    assert db_session.scalar(select(func.count()).select_from(LlmUsage)) == 0
    assert client.get("/benchmarks", headers=auth).status_code == 200


def test_operator_grant_identity_check_revocation_and_kill_switch(client, db_session, admission_env):
    user_id = register(client).json()["id"]
    auth = headers(client)
    with pytest.raises(ValueError):
        set_operator(db_session, user_id, "wrong@example.com", True)
    db_session.rollback()
    assert client.get("/auth/me", headers=auth).json()["chatEnabled"] is False
    set_operator(db_session, user_id, "visitor@example.com", True)
    assert client.get("/auth/me", headers=auth).json()["chatEnabled"] is True
    assert client.get("/auth/me", headers=auth).headers["cache-control"] == "no-store"
    admission_env.setenv("CHAT_ENABLED", "false")
    get_settings.cache_clear()
    assert client.get("/auth/me", headers=auth).json()["chatEnabled"] is False
    assert client.get("/projects/1/chat", headers=auth).status_code == 403
    admission_env.setenv("CHAT_ENABLED", "true")
    get_settings.cache_clear()
    set_operator(db_session, user_id, "visitor@example.com", False)
    assert client.get("/projects/1/chat", headers=auth).status_code == 403
    assert client.get("/auth/me", headers=auth).json()["chatEnabled"] is False


def test_shared_auth_bucket_concurrent_burst_and_refill(migrated_engine, admission_env):
    admission_env.setenv("AUTH_RATE_LIMIT_ENABLED", "true")
    admission_env.setenv("AUTH_REQUESTS_PER_MINUTE", "1")
    admission_env.setenv("AUTH_BURST", "4")
    get_settings.cache_clear()
    barrier = Barrier(12)
    def attempt(_):
        barrier.wait()
        with Session(migrated_engine) as db:
            try:
                limit_auth_requests(db)
                return 200
            except HTTPException as exc:
                assert exc.headers["Retry-After"] == "60"
                return exc.status_code
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(attempt, range(12)))
    assert results.count(200) == 4
    assert results.count(429) == 8
    with Session(migrated_engine) as db:
        assert db.scalar(text("SELECT count(*) FROM auth_rate_bucket")) == 1
        db.execute(text("UPDATE auth_rate_bucket SET updated_at = clock_timestamp() - interval '2 minutes'"))
        db.commit()
        limit_auth_requests(db)


def test_limit_spans_routes_hosts_and_ignores_forged_ip_headers(client, admission_env):
    admission_env.setenv("AUTH_RATE_LIMIT_ENABLED", "true")
    admission_env.setenv("AUTH_REQUESTS_PER_MINUTE", "1")
    admission_env.setenv("AUTH_BURST", "1")
    get_settings.cache_clear()
    assert client.post("/auth/login", json={"email": "none@example.com", "password": "x"}).status_code == 401
    result = client.post("/auth/register", json={"email": "new@example.com", "password": "x"},
                         headers={"Host": "alternate.example", "X-Forwarded-For": "1.2.3.4"})
    assert result.status_code == 429


def test_limiter_fails_closed_on_storage_error(admission_env):
    from sqlalchemy.exc import OperationalError
    admission_env.setenv("AUTH_RATE_LIMIT_ENABLED", "true")
    get_settings.cache_clear()
    db = Mock()
    db.execute.side_effect = OperationalError("query", {}, Exception("unavailable"))
    with pytest.raises(HTTPException) as error:
        limit_auth_requests(db)
    assert error.value.status_code == 503
    db.rollback.assert_called_once()
