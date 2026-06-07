"""S9 Jenkins connection tests: BYOK secrets stored as refs, never as plaintext.

The security guarantee is the point: the token + key go into the SecretStore and
only refs land on the jenkins_connection row — nothing plaintext in the DB or logs.
Plus owner-scoping and idempotent upsert.
"""

import logging

from sqlalchemy import func, select

from app.catalog.seed import load_seed
from app.models import JenkinsConnection, User
from app.secret_store import get_secret_store

TOKEN = "jenkins-api-token-SUPER-SECRET-abc123"
KEY = "sk-byok-model-key-SUPER-SECRET-xyz789"


def _register(client, db_session, email: str) -> tuple[dict[str, str], int]:
    creds = {"email": email, "password": "correct horse battery"}
    client.post("/auth/register", json=creds)
    token = client.post("/auth/login", json=creds).json()["accessToken"]
    user_id = db_session.scalar(select(User.id).where(User.email == email))
    return {"Authorization": f"Bearer {token}"}, user_id


def _make_project(client, headers) -> int:
    body = client.post(
        "/recommendations",
        json={"taskTypes": ["agentic_coding"], "budgetSensitivity": "high"},
        headers=headers,
    ).json()
    return client.post(
        "/projects",
        json={
            "name": "p",
            "selectedOptionId": body["shortlist"][0]["recommendationOptionId"],
            "baselineModelId": body["baseline"]["modelId"],
        },
        headers=headers,
    ).json()["id"]


def _payload() -> dict:
    return {
        "baseUrl": "http://jenkins.example.com:8080",
        "jobName": "modelmatch-review",
        "jenkinsToken": TOKEN,
        "modelApiKey": KEY,
    }


def test_connect_stores_refs_not_plaintext_in_response(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "j_ok@example.com")
    pid = _make_project(client, headers)

    resp = client.put(f"/projects/{pid}/jenkins", json=_payload(), headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "configured"
    assert body["baseUrl"] == "http://jenkins.example.com:8080"
    # response carries refs, never the secrets
    assert body["jenkinsTokenRef"] and body["modelApiKeyRef"]
    assert TOKEN not in resp.text and KEY not in resp.text


def test_no_plaintext_secret_in_any_db_column(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "j_db@example.com")
    pid = _make_project(client, headers)
    client.put(f"/projects/{pid}/jenkins", json=_payload(), headers=headers)

    conn = db_session.scalar(
        select(JenkinsConnection).where(JenkinsConnection.project_id == pid)
    )
    # scan every column on the row — no secret value anywhere
    all_values = " ".join(str(getattr(conn, c.name)) for c in conn.__table__.columns)
    assert TOKEN not in all_values
    assert KEY not in all_values
    # the refs are stored and are not the secrets themselves
    assert conn.jenkins_token_ref != TOKEN
    assert conn.model_api_key_ref != KEY


def test_secret_is_in_the_vault_only_ref_in_db(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "j_vault@example.com")
    pid = _make_project(client, headers)
    body = client.put(f"/projects/{pid}/jenkins", json=_payload(), headers=headers).json()

    store = get_secret_store()  # same process singleton the service used
    assert store.get(body["jenkinsTokenRef"]) == TOKEN
    assert store.get(body["modelApiKeyRef"]) == KEY


def test_secrets_not_written_to_logs(client, db_session, caplog):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "j_log@example.com")
    pid = _make_project(client, headers)
    with caplog.at_level(logging.DEBUG):
        client.put(f"/projects/{pid}/jenkins", json=_payload(), headers=headers)
    assert TOKEN not in caplog.text
    assert KEY not in caplog.text


def test_put_is_idempotent_upsert(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "j_upsert@example.com")
    pid = _make_project(client, headers)

    client.put(f"/projects/{pid}/jenkins", json=_payload(), headers=headers)
    second = {**_payload(), "jobName": "renamed-job", "jenkinsToken": "new-token-value"}
    resp = client.put(f"/projects/{pid}/jenkins", json=second, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["jobName"] == "renamed-job"

    n = db_session.scalar(
        select(func.count()).select_from(JenkinsConnection).where(
            JenkinsConnection.project_id == pid
        )
    )
    assert n == 1  # updated, not duplicated


def test_get_returns_status_without_secrets(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "j_get@example.com")
    pid = _make_project(client, headers)
    client.put(f"/projects/{pid}/jenkins", json=_payload(), headers=headers)

    resp = client.get(f"/projects/{pid}/jenkins", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "configured"
    assert TOKEN not in resp.text and KEY not in resp.text


def test_get_unconfigured_is_404(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "j_unconf@example.com")
    pid = _make_project(client, headers)
    assert client.get(f"/projects/{pid}/jenkins", headers=headers).status_code == 404


def test_connect_is_owner_scoped(client, db_session):
    load_seed(db_session)
    headers_a, _ = _register(client, db_session, "j_own_a@example.com")
    headers_b, _ = _register(client, db_session, "j_own_b@example.com")
    pid_a = _make_project(client, headers_a)

    # B cannot configure A's project
    resp = client.put(f"/projects/{pid_a}/jenkins", json=_payload(), headers=headers_b)
    assert resp.status_code == 403


def test_connect_unknown_project_404(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "j_404@example.com")
    assert client.put("/projects/999999/jenkins", json=_payload(), headers=headers).status_code == 404


def test_empty_secret_or_base_url_is_422(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "j_422@example.com")
    pid = _make_project(client, headers)

    assert client.put(f"/projects/{pid}/jenkins", json={**_payload(), "jenkinsToken": ""}, headers=headers).status_code == 422
    assert client.put(f"/projects/{pid}/jenkins", json={**_payload(), "baseUrl": ""}, headers=headers).status_code == 422


def test_malformed_base_url_is_422(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "j_url@example.com")
    pid = _make_project(client, headers)
    for bad in ("not-a-url", "ftp://jenkins.example.com", "jenkins.example.com:8080"):
        resp = client.put(f"/projects/{pid}/jenkins", json={**_payload(), "baseUrl": bad}, headers=headers)
        assert resp.status_code == 422, bad


def test_jenkins_requires_auth(client):
    assert client.put("/projects/1/jenkins", json=_payload()).status_code == 401
    assert client.get("/projects/1/jenkins").status_code == 401
