"""E20 (P38e): `GET /projects/{id}/agent-config` — the run-time contract the agent
consumes (HLD §3b.1, built by P38d against an in-process stub of exactly this body).

What matters:
- auth is the SAME per-project CI token path as /ci-runs: 404 unknown project, 401
  missing/invalid/unminted token, no JWT involved;
- the body is the contract verbatim: `task` (the agent's short name) + `taskType`
  (catalog vocabulary), the selected option's runtime-config row with a BARE
  provider model id and the credential env-var NAME, and `reviewPreferences` for
  the review task only (null for security projects, whatever is stored);
- the response never carries a token or a key.
"""

from sqlalchemy import select

from app.catalog.seed import load_seed
from app.models import Project
from app.tasks import CI_REVIEW, SECURITY_ANALYSIS
from tests.test_ci import (
    _connect_jenkins,
    _make_project,
    _mint_token,
    _register,
    _set_project_runtime_config,
)


def _make_security_project(client, headers) -> int:
    body = client.post(
        "/recommendations",
        json={"taskTypes": [SECURITY_ANALYSIS], "budgetSensitivity": "high"},
        headers=headers,
    ).json()
    return client.post(
        "/projects",
        json={
            "name": "sec",
            "selectedOptionId": body["shortlist"][0]["recommendationOptionId"],
            "baselineModelId": body["baseline"]["modelId"],
            "taskType": SECURITY_ANALYSIS,
        },
        headers=headers,
    ).json()["id"]


def test_agent_config_review_project_matches_the_contract(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ac_review@example.com")
    pid = _make_project(client, headers)  # ci_review → Claude Haiku 4.5
    client.patch(
        f"/projects/{pid}",
        json={"reviewPreferences": "Flag any use of eval(). Ignore import ordering."},
        headers=headers,
    )
    token = _mint_token(client, headers, pid)

    resp = client.get(f"/projects/{pid}/agent-config", headers={"X-CI-Token": token})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "projectId": pid,
        "task": "review",
        "taskType": CI_REVIEW,
        "model": {
            "name": "Claude Haiku 4.5",
            "provider": "anthropic",
            "providerModelId": "claude-haiku-4-5",  # BARE — the agent composes provider/id
            "authMode": "api_key",
            "credentialEnvVar": "ANTHROPIC_API_KEY",  # a NAME, never a value
        },
        "reviewPreferences": "Flag any use of eval(). Ignore import ordering.",
    }
    # nothing secret on the wire
    text = resp.text
    assert token not in text
    assert "ci_token" not in text and "hash" not in text


def test_agent_config_security_project_is_deepseek_with_null_preferences(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ac_sec@example.com")
    pid = _make_security_project(client, headers)
    # preferences stored on a security project are NOT served (review task only)
    client.patch(f"/projects/{pid}", json={"reviewPreferences": "ignored"}, headers=headers)
    token = _mint_token(client, headers, pid)

    body = client.get(f"/projects/{pid}/agent-config", headers={"X-CI-Token": token}).json()
    assert body["task"] == "security"
    assert body["taskType"] == SECURITY_ANALYSIS
    assert body["model"] == {
        "name": "DeepSeek V4 Flash",
        "provider": "deepseek",
        "providerModelId": "deepseek-v4-flash",
        "authMode": "api_key",
        "credentialEnvVar": "DEEPSEEK_API_KEY",
    }
    assert body["reviewPreferences"] is None


def test_agent_config_serves_the_selected_runtime_row_verbatim(client, db_session):
    """The model block is the runtime-config ROW, not a vendor guess: rewrite it and
    the endpoint follows (a Bedrock row has no credential variable at all)."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ac_row@example.com")
    pid = _make_project(client, headers)
    _set_project_runtime_config(
        db_session, pid,
        provider="bedrock", provider_model_id="global.amazon.nova-2-lite-v1:0",
        auth_mode="aws_iam", credential_env_var=None,
    )
    token = _mint_token(client, headers, pid)

    body = client.get(f"/projects/{pid}/agent-config", headers={"X-CI-Token": token}).json()
    assert body["model"]["provider"] == "bedrock"
    assert body["model"]["providerModelId"] == "global.amazon.nova-2-lite-v1:0"
    assert body["model"]["authMode"] == "aws_iam"
    assert body["model"]["credentialEnvVar"] is None


def test_agent_config_missing_token_is_401(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ac_401a@example.com")
    pid = _make_project(client, headers)
    _mint_token(client, headers, pid)
    assert client.get(f"/projects/{pid}/agent-config").status_code == 401


def test_agent_config_wrong_token_is_401(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ac_401b@example.com")
    pid = _make_project(client, headers)
    _mint_token(client, headers, pid)
    resp = client.get(f"/projects/{pid}/agent-config", headers={"X-CI-Token": "nope"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid or missing CI token"  # one opaque error


def test_agent_config_before_ci_setup_is_401(client, db_session):
    """No Jenkins connection / no minted token → unreachable, by design (BYOK: no
    token, no config). Connecting without minting is still 401."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ac_pre@example.com")
    pid = _make_project(client, headers)
    assert client.get(f"/projects/{pid}/agent-config", headers={"X-CI-Token": "x"}).status_code == 401
    _connect_jenkins(client, headers, pid)
    assert client.get(f"/projects/{pid}/agent-config", headers={"X-CI-Token": "x"}).status_code == 401


def test_agent_config_unknown_project_is_404(client, db_session):
    assert client.get("/projects/999999/agent-config", headers={"X-CI-Token": "x"}).status_code == 404


def test_agent_config_rotated_token_replaces_the_old_one(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ac_rot@example.com")
    pid = _make_project(client, headers)
    old = _mint_token(client, headers, pid)
    new = client.post(f"/projects/{pid}/ci-setup/rotate", headers=headers).json()["token"]
    assert client.get(f"/projects/{pid}/agent-config", headers={"X-CI-Token": old}).status_code == 401
    assert client.get(f"/projects/{pid}/agent-config", headers={"X-CI-Token": new}).status_code == 200


def test_agent_config_ignores_user_jwt(client, db_session):
    """A user session is NOT a CI token — the owner's JWT alone gets 401."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ac_jwt@example.com")
    pid = _make_project(client, headers)
    _mint_token(client, headers, pid)
    assert client.get(f"/projects/{pid}/agent-config", headers=headers).status_code == 401


def test_agent_config_follows_a_repick_to_the_other_task(client, db_session):
    """PATCH re-pick from a review option to a security option flips task + task
    type (the task follows the option) — the agent sees the change on its next run."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ac_repick@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)
    assert client.get(f"/projects/{pid}/agent-config", headers={"X-CI-Token": token}).json()["task"] == "review"

    rec = client.post(
        "/recommendations",
        json={"taskTypes": [SECURITY_ANALYSIS], "budgetSensitivity": "high"},
        headers=headers,
    ).json()
    resp = client.patch(
        f"/projects/{pid}",
        json={
            "selectedOptionId": rec["shortlist"][0]["recommendationOptionId"],
            "baselineModelId": rec["baseline"]["modelId"],
        },
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["taskType"] == SECURITY_ANALYSIS
    assert db_session.get(Project, pid).task_type == SECURITY_ANALYSIS
    body = client.get(f"/projects/{pid}/agent-config", headers={"X-CI-Token": token}).json()
    assert body["task"] == "security" and body["model"]["provider"] == "deepseek"


def test_agent_config_route_is_registered_next_to_ci_runs(client):
    paths = {r.path for r in client.app.routes}
    assert "/projects/{project_id}/agent-config" in paths
    assert "/projects/{project_id}/ci-runs" in paths
    # only the two projects-scoped CI-token routes exist, nothing exposes the token
    assert not any(p.endswith("/ci-token") for p in paths)
    _ = select  # keep the import honest for editors that flag unused names
