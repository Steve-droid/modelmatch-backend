"""Offline RS256 verification and Google/password persistence contracts."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select

from app.auth import google, security
from app.config import get_settings
from app.models import User
from app.models.orm import GoogleLoginNonce

ORIGIN = {"Origin": "http://localhost:5173"}
CLIENT_ID = "test-web-client.apps.googleusercontent.com"

@pytest.fixture
def google_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    get_settings.cache_clear()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(google, "_google_keys", lambda: SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key())))
    yield key
    get_settings.cache_clear()

def credential(key, expected_nonce, **changes):
    now = datetime.now(timezone.utc)
    claims = {"iss": "https://accounts.google.com", "aud": CLIENT_ID,
              "sub": "google-user-123", "email": "new@example.com", "email_verified": True,
              "nonce": expected_nonce, "iat": now, "exp": now + timedelta(minutes=10)}
    claims.update(changes)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "offline"})

def sign_in(client, key, **changes):
    state = client.post("/auth/google/challenge", json={}, headers=ORIGIN)
    assert state.status_code == 200
    payload = {"credential": credential(key, state.json()["nonce"], **changes),
               "challenge": state.json()["challenge"]}
    return client.post("/auth/google", json=payload, headers=ORIGIN), payload

def test_valid_signature_and_claims(google_env):
    assert google.verify_credential(credential(google_env, "nonce"), "nonce")["sub"] == "google-user-123"

@pytest.mark.parametrize("changes", [
    {"iss": "https://attacker.example"}, {"aud": "another-client"},
    {"exp": 1}, {"iat": 9999999999}, {"nonce": "wrong"},
    {"email_verified": False}, {"email_verified": "true"},
    {"sub": ""}, {"sub": 42}, {"azp": "another-client"}, {"email": "invalid"},
])
def test_invalid_claims_rejected(google_env, changes):
    with pytest.raises((jwt.PyJWTError, ValueError)):
        google.verify_credential(credential(google_env, "nonce", **changes), "nonce")

def test_forged_signature_rejected(google_env):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(jwt.InvalidSignatureError):
        google.verify_credential(credential(other, "nonce"), "nonce")

def test_registration_returning_login_and_no_password(client, db_session, google_env):
    first, _ = sign_in(client, google_env)
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    first_id = security.decode_access_token(first.json()["accessToken"])["sub"]
    again, _ = sign_in(client, google_env, email="changed@example.com")
    assert again.status_code == 200
    assert security.decode_access_token(again.json()["accessToken"])["sub"] == first_id
    assert db_session.scalar(select(func.count()).select_from(User)) == 1
    user = db_session.get(User, int(first_id))
    assert user.password_hash is None
    assert user.email == "new@example.com"
    assert client.post("/auth/login", json={"email": user.email, "password": ""}).status_code == 401
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {first.json()['accessToken']}"})
    assert me.json() == {"id": user.id, "email": user.email, "chatEnabled": False}

def test_password_account_never_linked(client, db_session, google_env):
    password = {"email": "New@example.com", "password": "original-password"}
    original = client.post("/auth/register", json=password).json()
    result, _ = sign_in(client, google_env)
    assert result.status_code == 409
    assert db_session.get(User, original["id"]).google_subject is None
    assert client.post("/auth/login", json=password).status_code == 200
    assert db_session.scalar(select(func.count()).select_from(User)) == 1

def test_other_google_subject_collision(client, google_env):
    assert sign_in(client, google_env)[0].status_code == 200
    assert sign_in(client, google_env, sub="different-person")[0].status_code == 409

def test_replay_rejected(client, google_env):
    first, payload = sign_in(client, google_env)
    assert first.status_code == 200
    assert client.post("/auth/google", json=payload, headers=ORIGIN).status_code == 401

def test_invalid_token_never_creates_user(client, db_session, google_env):
    assert sign_in(client, google_env, email_verified=False)[0].status_code == 401
    assert db_session.scalar(select(func.count()).select_from(User)) == 0
    assert db_session.scalar(select(func.count()).select_from(GoogleLoginNonce)) == 0

@pytest.mark.parametrize("origin", [None, "null", "https://attacker.example"])
def test_origin_required(client, google_env, origin):
    headers = {} if origin is None else {"Origin": origin}
    assert client.post("/auth/google/challenge", json={}, headers=headers).status_code == 403
    assert client.post("/auth/google", json={"credential": "x", "challenge": "x"}, headers=headers).status_code == 403

def test_form_post_rejected(client, google_env):
    assert client.post("/auth/google/challenge", data={"a": "b"}, headers=ORIGIN).status_code == 415

def test_disabled_config(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    get_settings.cache_clear()
    assert client.get("/auth/google/config").json() == {"enabled": False}
    assert client.post("/auth/google/challenge", json={}, headers=ORIGIN).status_code == 503

def test_expired_challenge_and_wrong_token_type(client, google_env):
    state = google.create_challenge()
    decoded = jwt.decode(state.challenge, options={"verify_signature": False})
    decoded["exp"] = 1
    expired = jwt.encode(decoded, get_settings().jwt_secret, algorithm="HS256")
    for challenge in [expired, state.challenge + "bad", security.create_access_token("1")]:
        response = client.post("/auth/google", json={
            "credential": credential(google_env, state.nonce), "challenge": challenge,
        }, headers=ORIGIN)
        assert response.status_code == 401
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {state.challenge}"}).status_code == 401

def test_outage_opaque(client, google_env, monkeypatch):
    def unavailable(*args):
        raise jwt.PyJWKClientConnectionError("sensitive provider internals")
    monkeypatch.setattr(google, "verify_credential", unavailable)
    result, _ = sign_in(client, google_env)
    assert result.status_code == 503
    assert "sensitive" not in result.text

def test_expired_nonces_cleaned(client, db_session, google_env):
    db_session.add(GoogleLoginNonce(nonce="old", expires_at=datetime.now(timezone.utc)-timedelta(hours=1)))
    db_session.commit()
    assert sign_in(client, google_env)[0].status_code == 200
    db_session.expire_all()
    assert db_session.get(GoogleLoginNonce, "old") is None

@pytest.mark.parametrize("claim", ["iss", "aud", "sub", "exp", "iat", "nonce", "email", "email_verified"])
def test_required_claims(google_env, claim):
    claims = jwt.decode(credential(google_env, "nonce"), options={"verify_signature": False})
    del claims[claim]
    token = jwt.encode(claims, google_env, algorithm="RS256")
    with pytest.raises(jwt.MissingRequiredClaimError):
        google.verify_credential(token, "nonce")

@pytest.mark.parametrize("reuse_challenge", [True, False])
def test_concurrent_signins(db_session, google_env, reuse_challenge):
    from concurrent.futures import ThreadPoolExecutor
    from fastapi import HTTPException
    from sqlalchemy.orm import Session
    state = google.create_challenge()
    states = [state, state if reuse_challenge else google.create_challenge()]
    def attempt(state):
        with Session(db_session.get_bind()) as db:
            try:
                google.authenticate_google(db, credential(google_env, state.nonce), state.challenge)
                return 200
            except HTTPException as exc:
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(attempt, states))
    assert codes == ([200, 401] if reuse_challenge else [200, 200])
    assert db_session.scalar(select(func.count()).select_from(User)) == 1
