"""S9/S15c Jenkins connection tests: metadata-only connection, no secrets stored.

Since S15c the connection is metadata only (base URL + job name). The provider key
and CI token live in the user's own Jenkins credentials, so the backend must NOT
accept or store any secret: a metadata-only PUT succeeds, sending a secret is a 422,
and nothing lands in the SecretStore. Plus owner-scoping and idempotent upsert.
"""

from sqlalchemy import func, select

from app.catalog.seed import load_seed
from app.models import JenkinsConnection, User
from app.secret_store import get_secret_store

# A secret a (buggy) old client might still try to send — must be rejected, never stored.
LEAKED = "sk-byok-model-key-SUPER-SECRET-xyz789"


def _register(client, db_session, email: str) -> tuple[dict[str, str], int]:
    creds = {"email": email, "password": "correct horse battery"}
    client.post("/auth/register", json=creds)
    token = client.post("/auth/login", json=creds).json()["accessToken"]
    user_id = db_session.scalar(select(User.id).where(User.email == email))
    return {"Authorization": f"Bearer {token}"}, user_id


def _make_project(client, headers) -> int:
    body = client.post(
        "/recommendations",
        json={"taskTypes": ["ci_review"], "budgetSensitivity": "high"},
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
    # Metadata only — no jenkinsToken, no modelApiKey.
    return {
        "baseUrl": "http://jenkins.example.com:8080",
        "jobName": "modelmatch-review",
    }


def test_connect_metadata_only_is_configured(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "j_ok@example.com")
    pid = _make_project(client, headers)

    resp = client.put(f"/projects/{pid}/jenkins", json=_payload(), headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "configured"
    assert body["baseUrl"] == "http://jenkins.example.com:8080"
    assert body["jobName"] == "modelmatch-review"
    # The response carries metadata only — no secret refs in the contract anymore.
    assert "jenkinsTokenRef" not in body
    assert "modelApiKeyRef" not in body


def test_no_secret_is_required_or_stored(client, db_session):
    """A metadata-only PUT must not write anything to the SecretStore, and the legacy
    ref columns must stay NULL — no placeholder secret is required or persisted."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "j_nosecret@example.com")
    pid = _make_project(client, headers)
    client.put(f"/projects/{pid}/jenkins", json=_payload(), headers=headers)

    conn = db_session.scalar(
        select(JenkinsConnection).where(JenkinsConnection.project_id == pid)
    )
    # No secret refs written for this connection.
    assert conn.jenkins_token_ref is None
    assert conn.model_api_key_ref is None

    # The SecretStore holds nothing under this project's secret names.
    store = get_secret_store()
    assert store.get(f"local://secret/project/{pid}/jenkins-token") is None
    assert store.get(f"local://secret/project/{pid}/model-api-key") is None


def test_put_rejects_jenkins_token_secret_422(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "j_rejtok@example.com")
    pid = _make_project(client, headers)

    resp = client.put(
        f"/projects/{pid}/jenkins",
        json={**_payload(), "jenkinsToken": LEAKED},
        headers=headers,
    )
    assert resp.status_code == 422  # extra='forbid' — secrets are not accepted
    # The 422 must NOT echo the submitted secret back (our validation-error handler
    # strips `input`), so a stray secret never appears in the response body.
    assert LEAKED not in resp.text

    # Nothing was persisted: no connection row, no stored secret.
    assert db_session.scalar(
        select(func.count()).select_from(JenkinsConnection).where(
            JenkinsConnection.project_id == pid
        )
    ) == 0
    assert get_secret_store().get(f"local://secret/project/{pid}/jenkins-token") is None


def test_put_rejects_model_api_key_secret_422(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "j_rejkey@example.com")
    pid = _make_project(client, headers)

    resp = client.put(
        f"/projects/{pid}/jenkins",
        json={**_payload(), "modelApiKey": LEAKED},
        headers=headers,
    )
    assert resp.status_code == 422
    assert LEAKED not in resp.text  # the secret is not echoed back in the 422 body
    assert get_secret_store().get(f"local://secret/project/{pid}/model-api-key") is None


def test_put_is_idempotent_upsert(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "j_upsert@example.com")
    pid = _make_project(client, headers)

    client.put(f"/projects/{pid}/jenkins", json=_payload(), headers=headers)
    second = {**_payload(), "jobName": "renamed-job"}
    resp = client.put(f"/projects/{pid}/jenkins", json=second, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["jobName"] == "renamed-job"

    n = db_session.scalar(
        select(func.count()).select_from(JenkinsConnection).where(
            JenkinsConnection.project_id == pid
        )
    )
    assert n == 1  # updated, not duplicated


def test_get_returns_status_metadata_only(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "j_get@example.com")
    pid = _make_project(client, headers)
    client.put(f"/projects/{pid}/jenkins", json=_payload(), headers=headers)

    resp = client.get(f"/projects/{pid}/jenkins", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "configured"
    assert "jenkinsTokenRef" not in body and "modelApiKeyRef" not in body


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


def test_empty_or_missing_required_is_422(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "j_422@example.com")
    pid = _make_project(client, headers)

    assert client.put(f"/projects/{pid}/jenkins", json={**_payload(), "jobName": ""}, headers=headers).status_code == 422
    assert client.put(f"/projects/{pid}/jenkins", json={**_payload(), "baseUrl": ""}, headers=headers).status_code == 422
    assert client.put(f"/projects/{pid}/jenkins", json={"baseUrl": "http://x.example.com"}, headers=headers).status_code == 422


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
