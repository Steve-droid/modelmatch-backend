"""S4 auth tests: register -> login -> protected route, and the 401/403/409 paths.

Route tests go through the TestClient (real argon2 + real JWT against the throwaway
DB); the owner guard and the crypto primitives are unit-tested directly.
"""

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.auth import security
from app.auth.deps import require_owner
from app.config import get_settings
from app.models import User

REGISTER = {"email": "dev@example.com", "password": "correct horse battery"}


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- happy path -----------------------------------------------------------

def test_register_login_me_happy_path(client):
    reg = client.post("/auth/register", json=REGISTER)
    assert reg.status_code == 201
    body = reg.json()
    assert body["email"] == REGISTER["email"]
    assert "id" in body
    # the password (or its hash) must never appear in a response
    assert "password" not in body and "passwordHash" not in body and "password_hash" not in body

    login = client.post("/auth/login", json=REGISTER)
    assert login.status_code == 200
    token = login.json()["accessToken"]
    assert login.json()["tokenType"] == "bearer"

    me = client.get("/auth/me", headers=_auth_header(token))
    assert me.status_code == 200
    assert me.json()["email"] == REGISTER["email"]
    assert me.json()["id"] == body["id"]


def test_register_persists_argon2_hash_not_plaintext(client, db_session):
    client.post("/auth/register", json=REGISTER)
    user = db_session.scalar(select(User).where(User.email == REGISTER["email"]))
    assert user.password_hash != REGISTER["password"]
    assert user.password_hash.startswith("$argon2")
    assert security.verify_password(user.password_hash, REGISTER["password"])


# --- 409 / 422 register edges ---------------------------------------------

def test_register_duplicate_email_conflicts(client):
    assert client.post("/auth/register", json=REGISTER).status_code == 201
    dup = client.post("/auth/register", json=REGISTER)
    assert dup.status_code == 409


def test_register_invalid_email_unprocessable(client):
    resp = client.post("/auth/register", json={"email": "not-an-email", "password": "x"})
    assert resp.status_code == 422


# --- 401 login ------------------------------------------------------------

def test_login_wrong_password_unauthorized(client):
    client.post("/auth/register", json=REGISTER)
    resp = client.post("/auth/login", json={**REGISTER, "password": "wrong"})
    assert resp.status_code == 401


def test_login_unknown_email_unauthorized(client):
    resp = client.post("/auth/login", json={"email": "nobody@example.com", "password": "x"})
    assert resp.status_code == 401


# --- 401 protected route --------------------------------------------------

def test_me_without_token_unauthorized(client):
    assert client.get("/auth/me").status_code == 401


def test_me_with_malformed_token_unauthorized(client):
    assert client.get("/auth/me", headers=_auth_header("not.a.jwt")).status_code == 401


def test_me_with_expired_token_unauthorized(client):
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expired = jwt.encode(
        {"sub": "1", "iat": now - timedelta(hours=2), "exp": now - timedelta(hours=1)},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    assert client.get("/auth/me", headers=_auth_header(expired)).status_code == 401


def test_me_with_token_for_unknown_user_unauthorized(client):
    token = security.create_access_token("999999")
    assert client.get("/auth/me", headers=_auth_header(token)).status_code == 401


# --- unit: owner guard (403) ----------------------------------------------

def test_require_owner_allows_owner():
    user = User(id=7, email="o@x.test", password_hash="h")
    assert require_owner(7, user) is None


def test_require_owner_forbids_other_user():
    user = User(id=7, email="o@x.test", password_hash="h")
    with pytest.raises(HTTPException) as exc:
        require_owner(8, user)
    assert exc.value.status_code == 403


# --- unit: crypto primitives ----------------------------------------------

def test_hash_is_salted_and_verifiable():
    h1 = security.hash_password("pw")
    h2 = security.hash_password("pw")
    assert h1 != h2  # random salt → different hashes
    assert security.verify_password(h1, "pw")
    assert not security.verify_password(h1, "nope")


def test_jwt_round_trip_and_tamper_rejected():
    token = security.create_access_token("42")
    assert security.decode_access_token(token)["sub"] == "42"
    with pytest.raises(jwt.PyJWTError):
        security.decode_access_token(token + "tampered")
