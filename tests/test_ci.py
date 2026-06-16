"""S11 CI integration tests: ci-setup snippet + per-project-token run ingest.

The guarantees that matter:
- ci-setup is owner-scoped, mints the token ONCE (plaintext returned only then),
  and the hash — never the plaintext — is what lands in the DB.
- /ci-runs is authed by the per-project token, NOT a user JWT: valid → persists a
  ci_run + its ci_finding rows (model = the project's selected, priced model);
  missing/invalid token → 401; unknown project → 404; bad body → 422; a re-POSTed
  build id → 409. Ingest needs no JWT at all.
"""

from pathlib import Path

import pytest
from sqlalchemy import func, select

import agent.__main__ as agent_cli
from agent.config import AgentConfig
from agent.review import review
from app.catalog.seed import load_seed
from app.ci.service import AGENT_DIFF_ARG, build_review_snippet
from app.ci.tokens import hash_token
from app.llm.fake import FakeLLMClient
from app.models import (
    AgentRuntimeConfig,
    CiFinding,
    CiRun,
    JenkinsConnection,
    Project,
    RecommendationOption,
    User,
)
from app.schemas.ci import MAX_FINDINGS, MAX_MESSAGE_LEN, MAX_TOKENS

ROOT = Path(__file__).resolve().parent.parent


def _register(client, db_session, email: str) -> tuple[dict[str, str], int]:
    creds = {"email": email, "password": "correct horse battery"}
    client.post("/auth/register", json=creds)
    token = client.post("/auth/login", json=creds).json()["accessToken"]
    user_id = db_session.scalar(select(User.id).where(User.email == email))
    return {"Authorization": f"Bearer {token}"}, user_id


def _make_project(client, headers) -> int:
    # ci_review is the demo path: the pick is Claude Haiku 4.5 and the configured
    # baseline (Claude Sonnet 4.5) is in the group, so savings are real (selected != baseline).
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


def _connect_jenkins(client, headers, pid: int) -> None:
    # Metadata-only connection (S15c): no secrets — the key + CI token live in the
    # user's Jenkins credentials. ci-setup still mints the per-project token from this.
    client.put(
        f"/projects/{pid}/jenkins",
        json={
            "baseUrl": "http://jenkins.example.com:8080",
            "jobName": "modelmatch-review",
        },
        headers=headers,
    )


def _set_project_runtime_config(
    db_session,
    pid: int,
    *,
    provider: str,
    provider_model_id: str,
    auth_mode: str,
    credential_env_var: str | None,
    enabled: bool = True,
) -> None:
    project = db_session.get(Project, pid)
    assert project is not None
    option = db_session.get(RecommendationOption, project.selected_option_id)
    assert option is not None
    config = db_session.scalar(
        select(AgentRuntimeConfig).where(AgentRuntimeConfig.model_id == option.model_id)
    )
    values = {
        "provider": provider,
        "provider_model_id": provider_model_id,
        "auth_mode": auth_mode,
        "credential_env_var": credential_env_var,
        "enabled": enabled,
    }
    if config is None:
        db_session.add(AgentRuntimeConfig(model_id=option.model_id, **values))
    else:
        for key, value in values.items():
            setattr(config, key, value)
    db_session.commit()


def _mint_token(client, headers, pid: int) -> str:
    """Configure Jenkins + fetch ci-setup → the freshly-minted plaintext token."""
    _connect_jenkins(client, headers, pid)
    return client.get(f"/projects/{pid}/ci-setup", headers=headers).json()["token"]


def _project_with_token(client, db_session, email: str) -> tuple[int, str]:
    headers, _ = _register(client, db_session, email)
    pid = _make_project(client, headers)
    return pid, _mint_token(client, headers, pid)


def _agent_result(build_id: str = "42") -> dict:
    return {
        "jenkinsBuildId": build_id,
        "model": "claude-haiku-4-5",
        "tokensIn": 1200,
        "tokensOut": 340,
        "gate": "pass",
        "gateReason": None,
        "findings": [
            {
                "severity": "high",
                "category": "security",
                "file": "app/db.py",
                "line": 12,
                "message": "Possible SQL injection in query builder",
            },
            {
                "severity": "low",
                "category": "style",
                "file": "app/util.py",
                "line": None,
                "message": "Variable name could be clearer",
            },
        ],
    }


# --- ci-setup -------------------------------------------------------------

def test_ci_setup_returns_snippet_and_mints_token_once(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_setup@example.com")
    pid = _make_project(client, headers)
    _connect_jenkins(client, headers, pid)

    first = client.get(f"/projects/{pid}/ci-setup", headers=headers)
    assert first.status_code == 200
    body = first.json()
    assert body["token"]  # plaintext returned on the first fetch
    assert body["ciRunsUrl"].endswith(f"/projects/{pid}/ci-runs")
    assert body["imageRef"] in body["snippet"]
    assert "X-CI-Token" in body["snippet"]

    # Mint-once: a second fetch returns the snippet but NO plaintext token.
    second = client.get(f"/projects/{pid}/ci-setup", headers=headers).json()
    assert second["token"] is None
    assert second["snippet"]


def test_ci_setup_hash_in_db_not_plaintext(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_hash@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)

    conn = db_session.scalar(
        select(JenkinsConnection).where(JenkinsConnection.project_id == pid)
    )
    assert conn.ci_token_hash == hash_token(token)
    assert conn.ci_token_hash != token  # never the plaintext
    all_values = " ".join(str(getattr(conn, c.name)) for c in conn.__table__.columns)
    assert token not in all_values


def test_ci_setup_mints_from_metadata_only_connection(client, db_session):
    """S15c: a connection created from metadata alone (no secret refs) is enough to
    mint the per-project CI token — no stored Jenkins/model secret is needed."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_meta@example.com")
    pid = _make_project(client, headers)
    _connect_jenkins(client, headers, pid)  # metadata only

    conn = db_session.scalar(
        select(JenkinsConnection).where(JenkinsConnection.project_id == pid)
    )
    assert conn.jenkins_token_ref is None and conn.model_api_key_ref is None
    assert conn.ci_token_hash is None  # not minted yet

    resp = client.get(f"/projects/{pid}/ci-setup", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["token"]  # token minted despite no stored secrets

    db_session.expire(conn)
    assert conn.ci_token_hash is not None  # only the hash persisted


def test_ci_setup_requires_jenkins_connection(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_noconn@example.com")
    pid = _make_project(client, headers)
    # No PUT /jenkins → no connection row to hang the token on.
    assert client.get(f"/projects/{pid}/ci-setup", headers=headers).status_code == 404


def test_ci_setup_is_owner_scoped(client, db_session):
    load_seed(db_session)
    headers_a, _ = _register(client, db_session, "ci_own_a@example.com")
    headers_b, _ = _register(client, db_session, "ci_own_b@example.com")
    pid_a = _make_project(client, headers_a)
    _connect_jenkins(client, headers_a, pid_a)
    assert client.get(f"/projects/{pid_a}/ci-setup", headers=headers_b).status_code == 403


def test_ci_setup_requires_auth(client):
    assert client.get("/projects/1/ci-setup").status_code == 401


# --- ci-token rotation (recovery for a lost mint-once token) ------------------

def test_ci_token_rotate_issues_a_fresh_token(client, db_session):
    """Rotation is the recovery path: it returns a NEW plaintext token, replaces the
    stored hash, and the previous token stops working."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_rotate@example.com")
    pid = _make_project(client, headers)
    _connect_jenkins(client, headers, pid)

    first = _mint_token(client, headers, pid)  # original mint-once token

    resp = client.post(f"/projects/{pid}/ci-setup/rotate", headers=headers)
    assert resp.status_code == 200
    rotated = resp.json()["token"]
    assert rotated and rotated != first  # a different, freshly-minted token

    conn = db_session.scalar(
        select(JenkinsConnection).where(JenkinsConnection.project_id == pid)
    )
    db_session.expire(conn)
    assert conn.ci_token_hash == hash_token(rotated)  # new token is authoritative
    assert conn.ci_token_hash != hash_token(first)  # old token no longer valid


def test_ci_token_rotate_works_when_never_minted(client, db_session):
    """Rotation also mints for a connection that never called ci-setup (still recovers
    a usable token); a subsequent GET stays mint-once (token None)."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_rotate_fresh@example.com")
    pid = _make_project(client, headers)
    _connect_jenkins(client, headers, pid)

    rotated = client.post(f"/projects/{pid}/ci-setup/rotate", headers=headers).json()["token"]
    assert rotated
    after = client.get(f"/projects/{pid}/ci-setup", headers=headers).json()
    assert after["token"] is None  # already minted → not re-shown


def test_ci_token_rotate_requires_connection_404(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_rotate_noconn@example.com")
    pid = _make_project(client, headers)
    assert client.post(f"/projects/{pid}/ci-setup/rotate", headers=headers).status_code == 404


def test_ci_token_rotate_is_owner_scoped(client, db_session):
    load_seed(db_session)
    headers_a, _ = _register(client, db_session, "ci_rotate_a@example.com")
    headers_b, _ = _register(client, db_session, "ci_rotate_b@example.com")
    pid_a = _make_project(client, headers_a)
    _connect_jenkins(client, headers_a, pid_a)
    assert client.post(f"/projects/{pid_a}/ci-setup/rotate", headers=headers_b).status_code == 403


def test_ci_token_rotate_requires_auth(client):
    assert client.post("/projects/1/ci-setup/rotate").status_code == 401


# --- ci-runs ingest -------------------------------------------------------

def test_ingest_persists_run_and_findings(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_ingest@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)

    # No Authorization header — ingest is token-authed only.
    resp = client.post(
        f"/projects/{pid}/ci-runs",
        json=_agent_result("build-7"),
        headers={"X-CI-Token": token},
    )
    assert resp.status_code == 201
    out = resp.json()
    assert out["jenkinsBuildId"] == "build-7"
    assert out["findingsCount"] == 2
    assert out["tokensIn"] == 1200 and out["tokensOut"] == 340
    assert out["gate"] == "pass"

    run = db_session.scalar(select(CiRun).where(CiRun.project_id == pid))
    assert run is not None
    assert run.task == "code_review"
    assert run.gate == "pass"  # audit trail persisted
    # S12 now fills the savings trio at ingest (see test_savings.py for the math);
    # quality_ok (S13, acceptance-rate gate) stays untouched on insert.
    assert run.actual_cost is not None and run.baseline_cost is not None
    assert run.savings == run.baseline_cost - run.actual_cost
    assert run.quality_ok is None

    # model_id = the project's selected (priced) catalog model
    project = db_session.get(Project, pid)
    option = db_session.get(RecommendationOption, project.selected_option_id)
    assert run.model_id == option.model_id

    findings = db_session.scalars(
        select(CiFinding).where(CiFinding.ci_run_id == run.id).order_by(CiFinding.severity)
    ).all()
    assert {f.category for f in findings} == {"security", "style"}
    assert all(f.ci_run_id == run.id for f in findings)


def test_ingest_missing_token_is_401(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_notok@example.com")
    pid = _make_project(client, headers)
    _mint_token(client, headers, pid)
    assert client.post(f"/projects/{pid}/ci-runs", json=_agent_result()).status_code == 401


def test_ingest_invalid_token_is_401(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_badtok@example.com")
    pid = _make_project(client, headers)
    _mint_token(client, headers, pid)
    resp = client.post(
        f"/projects/{pid}/ci-runs",
        json=_agent_result(),
        headers={"X-CI-Token": "not-the-real-token"},
    )
    assert resp.status_code == 401


def test_ingest_unknown_project_is_404(client, db_session):
    load_seed(db_session)
    resp = client.post(
        "/projects/999999/ci-runs",
        json=_agent_result(),
        headers={"X-CI-Token": "anything"},
    )
    assert resp.status_code == 404


def test_ingest_bad_body_is_422(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_422@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)
    bad = _agent_result()
    del bad["tokensIn"]  # required field missing
    resp = client.post(
        f"/projects/{pid}/ci-runs", json=bad, headers={"X-CI-Token": token}
    )
    assert resp.status_code == 422


def test_ingest_duplicate_build_id_is_409(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_dup@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)
    hdr = {"X-CI-Token": token}

    first = client.post(f"/projects/{pid}/ci-runs", json=_agent_result("dup-1"), headers=hdr)
    assert first.status_code == 201
    second = client.post(f"/projects/{pid}/ci-runs", json=_agent_result("dup-1"), headers=hdr)
    assert second.status_code == 409

    n = db_session.scalar(
        select(func.count()).select_from(CiRun).where(CiRun.project_id == pid)
    )
    assert n == 1  # the duplicate was not persisted


# --- snippet / CLI drift guard -------------------------------------------

def test_snippet_runs_image_via_docker_run_not_docker_agent(client, db_session, tmp_path, monkeypatch):
    """The snippet must invoke the image with `docker run … --diff pr.diff`, NOT a
    Jenkins `agent { docker { image } }` block (the image has an executable entrypoint
    that can't host the build's shell steps). And the appended args must match the CLI."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_drift@example.com")
    pid = _make_project(client, headers)
    _connect_jenkins(client, headers, pid)
    snippet = client.get(f"/projects/{pid}/ci-setup", headers=headers).json()["snippet"]

    assert "docker run" in snippet
    assert "agent { docker" not in snippet  # NOT a docker-agent block
    assert "agent any" in snippet           # a normal node
    assert AGENT_DIFF_ARG in snippet        # `--diff pr.diff` appended to the image

    # The image entrypoint is python -m agent, so those appended args reach the CLI.
    dockerfile = (ROOT / "agent" / "Dockerfile").read_text()
    assert 'ENTRYPOINT ["python", "-m", "agent"]' in dockerfile

    # And the CLI actually accepts `--diff <file>` (fake client, tiny diff).
    monkeypatch.setenv("LLM_CLIENT", "fake")
    diff_file = tmp_path / "pr.diff"
    diff_file.write_text("diff --git a/x.py b/x.py\n+print('hi')\n")
    assert agent_cli.main(["--diff", str(diff_file)]) in (0, 1)  # ran; gate pass/fail
    with pytest.raises(SystemExit):  # a drifted 'review' subcommand would not parse
        agent_cli.main(["review", "--diff", str(diff_file)])


def test_snippet_wires_real_provider_and_never_runs_fake(client, db_session):
    """The snippet must set explicit provider config (LLM_CLIENT/AGENT_MODEL/caps) +
    a BYOK key credential — and must NOT silently run the fake client."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_prov@example.com")
    pid = _make_project(client, headers)
    _set_project_runtime_config(
        db_session,
        pid,
        provider="anthropic",
        provider_model_id="claude-haiku-4-5",
        auth_mode="api_key",
        credential_env_var="ANTHROPIC_API_KEY",
    )
    _connect_jenkins(client, headers, pid)
    snippet = client.get(f"/projects/{pid}/ci-setup", headers=headers).json()["snippet"]

    assert "LLM_CLIENT=fake" not in snippet
    assert "LLM_CLIENT=anthropic" in snippet       # the configured demo default
    for knob in ("AGENT_MODEL=", "AGENT_MAX_TOKENS=", "AGENT_TOKEN_CEILING="):
        assert knob in snippet
    # BYOK key: bound to the SDK env var via a Jenkins credential, passed to docker
    # BY NAME (no value) — the secret must never be expanded into the command argv.
    assert "ANTHROPIC_API_KEY = credentials('modelmatch-model-api-key')" in snippet
    assert "-e ANTHROPIC_API_KEY \\" in snippet
    assert 'ANTHROPIC_API_KEY="$' not in snippet        # no value expansion in argv
    assert "$MODELMATCH_MODEL_API_KEY" not in snippet


def test_ci_setup_uses_selected_model_runtime_config_not_global_fallback(
    client, db_session, monkeypatch
):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_runtime_db@example.com")
    pid = _make_project(client, headers)
    _set_project_runtime_config(
        db_session,
        pid,
        provider="anthropic",
        provider_model_id="claude-from-db-runtime",
        auth_mode="api_key",
        credential_env_var="ANTHROPIC_API_KEY",
    )
    _connect_jenkins(client, headers, pid)
    monkeypatch.setenv("CI_AGENT_LLM_CLIENT", "gemini")
    monkeypatch.setenv("CI_AGENT_MODEL", "gemini-global-fallback")

    snippet = client.get(f"/projects/{pid}/ci-setup", headers=headers).json()["snippet"]

    assert "LLM_CLIENT=anthropic" in snippet
    assert "AGENT_MODEL=claude-from-db-runtime" in snippet
    assert "LLM_CLIENT=gemini" not in snippet
    assert "gemini-global-fallback" not in snippet


def test_ci_setup_bedrock_runtime_config_uses_nova_without_byok_key(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_runtime_bedrock@example.com")
    pid = _make_project(client, headers)
    _set_project_runtime_config(
        db_session,
        pid,
        provider="bedrock",
        provider_model_id="global.amazon.nova-2-lite-v1:0",
        auth_mode="aws_iam",
        credential_env_var=None,
    )
    _connect_jenkins(client, headers, pid)

    snippet = client.get(f"/projects/{pid}/ci-setup", headers=headers).json()["snippet"]

    assert "LLM_CLIENT=bedrock" in snippet
    assert "AGENT_MODEL=global.amazon.nova-2-lite-v1:0" in snippet
    assert "AWS_REGION=" in snippet
    assert "modelmatch-model-api-key" not in snippet
    assert "ANTHROPIC_API_KEY" not in snippet
    assert "GEMINI_API_KEY" not in snippet
    assert "GOOGLE_API_KEY" not in snippet


@pytest.mark.parametrize(
    "provider,provider_model_id,credential_env_var,absent_env",
    [
        ("anthropic", "claude-runtime-from-db", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"),
        ("gemini", "gemini-runtime-from-db", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"),
    ],
)
def test_ci_setup_api_key_runtime_configs_bind_provider_env_by_name(
    client,
    db_session,
    provider,
    provider_model_id,
    credential_env_var,
    absent_env,
):
    load_seed(db_session)
    headers, _ = _register(client, db_session, f"ci_runtime_{provider}@example.com")
    pid = _make_project(client, headers)
    _set_project_runtime_config(
        db_session,
        pid,
        provider=provider,
        provider_model_id=provider_model_id,
        auth_mode="api_key",
        credential_env_var=credential_env_var,
    )
    _connect_jenkins(client, headers, pid)

    snippet = client.get(f"/projects/{pid}/ci-setup", headers=headers).json()["snippet"]

    assert f"LLM_CLIENT={provider}" in snippet
    assert f"AGENT_MODEL={provider_model_id}" in snippet
    assert f"{credential_env_var} = credentials('modelmatch-model-api-key')" in snippet
    assert f"-e {credential_env_var} \\" in snippet
    assert f'{credential_env_var}="$' not in snippet
    assert f"-e {credential_env_var}=$" not in snippet
    assert absent_env not in snippet
    if provider == "gemini":
        assert "GEMINI_API_KEY" not in snippet


def test_snippet_keeps_ci_token_out_of_argv(client, db_session):
    """The CI token must not be a `-H` argument (visible in ps) — it goes through a
    curl config file built by a heredoc."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_argv@example.com")
    pid = _make_project(client, headers)
    _connect_jenkins(client, headers, pid)
    snippet = client.get(f"/projects/{pid}/ci-setup", headers=headers).json()["snippet"]

    assert '-H "X-CI-Token:' not in snippet              # not in curl argv
    assert "curl -fsS --config" in snippet               # consumed from a config file
    assert "mktemp" in snippet and "X-CI-Token: $MODELMATCH_CI_TOKEN" in snippet


def test_snippet_diff_is_pr_safe(client, db_session):
    """The diff must use CHANGE_TARGET (multibranch PR) with a main fallback — not a
    hardcoded origin/main."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_prdiff@example.com")
    pid = _make_project(client, headers)
    _connect_jenkins(client, headers, pid)
    snippet = client.get(f"/projects/{pid}/ci-setup", headers=headers).json()["snippet"]

    assert "${CHANGE_TARGET:-main}" in snippet
    assert "git fetch --no-tags origin" in snippet
    assert 'git diff "origin/${TARGET}...HEAD"' in snippet
    assert "origin/main...HEAD" not in snippet           # not hardcoded


def test_bedrock_snippet_uses_aws_creds_not_an_api_key():
    """The Bedrock variant wires AWS creds (region + node role/profile), no API key."""
    snippet = build_review_snippet(
        ci_runs_url="http://backend/projects/1/ci-runs",
        image_ref="modelmatch-agent:latest",
        llm_client="bedrock",
        model="global.amazon.nova-2-lite-v1:0",
        max_tokens=512,
        token_ceiling=4000,
        aws_region="ap-south-1",
    )
    assert "LLM_CLIENT=bedrock" in snippet and "LLM_CLIENT=fake" not in snippet
    assert "AGENT_MODEL=global.amazon.nova-2-lite-v1:0" in snippet
    assert "AWS_REGION=ap-south-1" in snippet
    assert "modelmatch-model-api-key" not in snippet  # no static key credential
    assert "ANTHROPIC_API_KEY" not in snippet
    assert "GEMINI_API_KEY" not in snippet and "GOOGLE_API_KEY" not in snippet


# --- untrusted-input validation (ABC: LLM output is untrusted) ------------

def test_ingest_rejects_negative_tokens_422(client, db_session):
    load_seed(db_session)
    pid, token = _project_with_token(client, db_session, "ci_neg@example.com")
    body = {**_agent_result(), "tokensIn": -1}
    assert client.post(f"/projects/{pid}/ci-runs", json=body, headers={"X-CI-Token": token}).status_code == 422


def test_ingest_rejects_absurd_token_count_422(client, db_session):
    load_seed(db_session)
    pid, token = _project_with_token(client, db_session, "ci_absurd@example.com")
    body = {**_agent_result(), "tokensOut": MAX_TOKENS + 1}
    assert client.post(f"/projects/{pid}/ci-runs", json=body, headers={"X-CI-Token": token}).status_code == 422


def test_ingest_rejects_absolute_file_path_422(client, db_session):
    load_seed(db_session)
    pid, token = _project_with_token(client, db_session, "ci_abs@example.com")
    body = _agent_result()
    body["findings"][0]["file"] = "/etc/passwd"  # absolute → must be rejected
    assert client.post(f"/projects/{pid}/ci-runs", json=body, headers={"X-CI-Token": token}).status_code == 422


def test_ingest_rejects_path_traversal_422(client, db_session):
    load_seed(db_session)
    pid, token = _project_with_token(client, db_session, "ci_trav@example.com")
    body = _agent_result()
    body["findings"][0]["file"] = "../../etc/shadow"
    assert client.post(f"/projects/{pid}/ci-runs", json=body, headers={"X-CI-Token": token}).status_code == 422


def test_ingest_rejects_oversized_message_422(client, db_session):
    load_seed(db_session)
    pid, token = _project_with_token(client, db_session, "ci_big@example.com")
    body = _agent_result()
    body["findings"][0]["message"] = "x" * (MAX_MESSAGE_LEN + 1)
    assert client.post(f"/projects/{pid}/ci-runs", json=body, headers={"X-CI-Token": token}).status_code == 422


def test_ingest_rejects_too_many_findings_422(client, db_session):
    load_seed(db_session)
    pid, token = _project_with_token(client, db_session, "ci_many@example.com")
    one = {"severity": "low", "category": "style", "file": "a.py", "line": 1, "message": "m"}
    body = {**_agent_result(), "findings": [one] * (MAX_FINDINGS + 1)}
    assert client.post(f"/projects/{pid}/ci-runs", json=body, headers={"X-CI-Token": token}).status_code == 422


def test_ingest_rejects_bad_build_id_422(client, db_session):
    load_seed(db_session)
    pid, token = _project_with_token(client, db_session, "ci_buildid@example.com")
    body = {**_agent_result(), "jenkinsBuildId": "has spaces & bad chars"}
    assert client.post(f"/projects/{pid}/ci-runs", json=body, headers={"X-CI-Token": token}).status_code == 422


def test_ingest_rejects_raw_diff_field_and_never_stores_it(client, db_session):
    """A raw diff (or any unexpected field) is rejected — never accepted or stored."""
    load_seed(db_session)
    pid, token = _project_with_token(client, db_session, "ci_diff@example.com")
    body = {**_agent_result("with-diff"), "diff": "diff --git a/secret.py b/secret.py\n+API_KEY='sk-leak'"}
    resp = client.post(f"/projects/{pid}/ci-runs", json=body, headers={"X-CI-Token": token})
    assert resp.status_code == 422  # extra='forbid' rejects it
    # The 422 must not echo the rejected diff back either (handler strips `input`).
    assert "diff --git" not in resp.text and "sk-leak" not in resp.text

    # Nothing was persisted, and no diff content leaked into any ci_run/ci_finding column.
    assert db_session.scalar(select(func.count()).select_from(CiRun).where(CiRun.project_id == pid)) == 0
    run_cols = " ".join(
        str(v) for r in db_session.scalars(select(CiRun)).all()
        for v in (getattr(r, c.name) for c in r.__table__.columns)
    )
    assert "diff --git" not in run_cols and "sk-leak" not in run_cols


# --- fake full-loop smoke (no real model spend) --------------------------

def test_fake_full_loop_smoke(client, db_session, tmp_path):
    """ci-setup → run the agent (FAKE client) on a tiny diff → POST the result →
    verify the persisted run, findings, and gate. No real Bedrock/Anthropic spend."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_smoke@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)

    diff = (
        "diff --git a/app/db.py b/app/db.py\n"
        "@@ -1 +1 @@\n"
        "+query = 'SELECT * FROM t WHERE id=' + user_id\n"
    )
    # The agent runs offline (fake client), scripted to find one high-severity issue
    # so the gate fails — exactly what the agent image emits to stdout.
    canned = (
        '{"findings": [{"severity": "high", "category": "security", '
        '"file": "app/db.py", "line": 1, '
        '"message": "SQL injection via string concatenation"}]}'
    )
    result = review(diff, FakeLLMClient(responses=canned, model="claude-haiku-4-5"), AgentConfig())
    assert result.gate == "fail"  # high severity → blocking

    # POST exactly what the agent produced (+ the build id), as the snippet does.
    payload = result.model_dump(by_alias=True)
    payload["jenkinsBuildId"] = "smoke-1"
    resp = client.post(f"/projects/{pid}/ci-runs", json=payload, headers={"X-CI-Token": token})
    assert resp.status_code == 201

    run = db_session.scalar(select(CiRun).where(CiRun.project_id == pid))
    assert run.gate == "fail" and run.gate_reason  # audit trail
    assert run.tokens_in > 0 and run.tokens_out > 0
    project = db_session.get(Project, pid)
    option = db_session.get(RecommendationOption, project.selected_option_id)
    assert run.model_id == option.model_id  # priced catalog model, not "claude-haiku-4-5"

    findings = db_session.scalars(select(CiFinding).where(CiFinding.ci_run_id == run.id)).all()
    assert len(findings) == 1
    assert findings[0].category == "security" and findings[0].severity == "high"
