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
from app.ci.service import (
    AGENT_DIFF_ARG,
    SECURITY_WORKSPACE,
    build_review_snippet,
    build_security_snippet,
)
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
from app.schemas.ci import MAX_CWE_LEN, MAX_FINDINGS, MAX_MESSAGE_LEN, MAX_TOKENS
from app.tasks import CI_REVIEW, SECURITY_ANALYSIS

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
    # the CI token is bound from the Jenkins credential and passed to the agent by NAME
    assert "MODELMATCH_CI_TOKEN = credentials('modelmatch-ci-token')" in body["snippet"]
    assert body["taskType"] == CI_REVIEW and body["task"] == "review"

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
    assert run.task == CI_REVIEW  # the run records the project's task (one vocabulary)
    assert run.cache_read_tokens is None  # a v1 agent never sends it
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

_SECURITY_SANDBOX = (
    "--cap-drop ALL --security-opt no-new-privileges",
    "--memory 2g --cpus 2 --tmpfs /tmp:size=256m",
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
    assert '-v "$PWD:/work" -w /work' in snippet  # the review image reads the diff here

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


def test_snippet_is_fetch_config_and_the_agent_posts_the_run(client, db_session):
    """E20 (HLD §3b.1): the stage sets the MODELMATCH_* trio + POST_RESULT + BUILD_TAG
    so the agent fetches task/model/preferences from the API and POSTs /ci-runs
    itself. Consequently the stage carries NO provider/model lines (they live in the
    runtime-config row, served by agent-config) and NEVER curls (one poster per stage
    — a second POST is a 409)."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_fetch@example.com")
    pid = _make_project(client, headers)
    _connect_jenkins(client, headers, pid)
    body = client.get(f"/projects/{pid}/ci-setup", headers=headers).json()
    snippet = body["snippet"]

    api_url = body["ciRunsUrl"].removesuffix(f"/projects/{pid}/ci-runs")
    assert f"-e MODELMATCH_API_URL={api_url} \\" in snippet
    assert f"-e MODELMATCH_PROJECT_ID={pid} \\" in snippet
    assert "-e MODELMATCH_CI_TOKEN \\" in snippet         # by NAME, no value in argv
    assert "-e MODELMATCH_POST_RESULT=true \\" in snippet
    assert "-e BUILD_TAG \\" in snippet                   # the run's jenkinsBuildId
    assert "MODELMATCH_CI_TOKEN=$" not in snippet and 'MODELMATCH_CI_TOKEN="$' not in snippet

    # no v1 provider wiring, no fake client, no second poster
    for gone in ("LLM_CLIENT=", "AGENT_MODEL=", "curl", "jq ", "X-CI-Token", "result.json", "payload.json"):
        assert gone not in snippet, gone
    # the review caps still ride along (they are ceilings, never the model)
    assert "-e AGENT_MAX_TOKENS=" in snippet and "-e AGENT_TOKEN_CEILING=" in snippet
    # the exit table: only 0 is a pass, and a refusal (3) is named as NOT a pass
    assert 'case "$AGENT_RC" in' in snippet
    assert "REFUSED" in snippet and "exit $AGENT_RC" in snippet


def test_ci_setup_api_key_credential_binding_follows_the_runtime_config(client, db_session):
    """The BYOK key is bound to whatever env var the selected model's runtime-config
    row declares — rewrite the row and the binding follows (not a vendor guess)."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_runtime_cfg@example.com")
    pid = _make_project(client, headers)
    _set_project_runtime_config(
        db_session, pid,
        provider="anthropic", provider_model_id="claude-from-db-runtime",
        auth_mode="api_key", credential_env_var="ANTHROPIC_API_KEY",
    )
    _connect_jenkins(client, headers, pid)
    snippet = client.get(f"/projects/{pid}/ci-setup", headers=headers).json()["snippet"]
    assert "ANTHROPIC_API_KEY = credentials('modelmatch-model-api-key')" in snippet
    assert "-e ANTHROPIC_API_KEY \\" in snippet
    assert 'ANTHROPIC_API_KEY="$' not in snippet        # no value expansion in argv
    assert "$MODELMATCH_MODEL_API_KEY" not in snippet
    assert "GOOGLE_API_KEY" not in snippet and "GEMINI_API_KEY" not in snippet


def test_ci_setup_bedrock_runtime_config_uses_nova_without_byok_key(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_bedrock_runtime@example.com")
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

    assert "AWS_DEFAULT_REGION=" in snippet
    assert "AWS_REGION=" in snippet
    assert '-v "$HOME/.aws:/home/appuser/.aws:ro"' in snippet
    assert "modelmatch-model-api-key" not in snippet
    assert "ANTHROPIC_API_KEY" not in snippet
    assert "GEMINI_API_KEY" not in snippet
    assert "GOOGLE_API_KEY" not in snippet


@pytest.mark.parametrize(
    "provider, provider_model_id, credential_env_var, absent_env",
    [
        ("anthropic", "claude-haiku-4-5", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"),
        ("gemini", "gemini-2.5-flash", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"),
    ],
)
def test_ci_setup_api_key_runtime_configs_bind_provider_env_by_name(
    client, db_session, provider, provider_model_id, credential_env_var, absent_env
):
    load_seed(db_session)
    headers, _ = _register(client, db_session, f"ci_{provider}_runtime@example.com")
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

    assert f"{credential_env_var} = credentials('modelmatch-model-api-key')" in snippet
    assert f"-e {credential_env_var} \\" in snippet
    assert f'{credential_env_var}="$' not in snippet
    assert f"-e {credential_env_var}=$" not in snippet
    assert absent_env not in snippet
    if provider == "gemini":
        assert "GEMINI_API_KEY" not in snippet


def test_snippet_diff_is_pr_safe(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_prsafe@example.com")
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
        api_url="http://backend",
        project_id=1,
        image_ref="modelmatch-agent:latest",
        max_tokens=512,
        token_ceiling=4000,
        aws_region="ap-south-1",
        auth_mode="aws_iam",
    )
    assert "AWS_DEFAULT_REGION=ap-south-1" in snippet
    assert "AWS_REGION=ap-south-1" in snippet
    assert "modelmatch-model-api-key" not in snippet  # no static key credential
    assert "ANTHROPIC_API_KEY" not in snippet
    assert "GEMINI_API_KEY" not in snippet and "GOOGLE_API_KEY" not in snippet
    assert "-e MODELMATCH_API_URL=http://backend \\" in snippet


# --- E20: the security task's snippet + per-task image -----------------------

def test_security_project_gets_the_security_stage(client, db_session, monkeypatch):
    """A security_analysis project's snippet runs the SECURITY image over a READ-ONLY
    checkout under the sandbox flags, takes no args (no diff), sets the same
    MODELMATCH_* trio, binds the runtime row's credential (DeepSeek → DEEPSEEK_API_KEY)
    and never curls."""
    monkeypatch.setenv("AGENT_IMAGE", "registry/modelmatch-agent:1.1.0")
    monkeypatch.setenv("AGENT_SECURITY_IMAGE", "registry/modelmatch-agent-security:1.1.0")
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        load_seed(db_session)
        headers, _ = _register(client, db_session, "ci_sec@example.com")
        pid = _make_security_project(client, headers)
        _connect_jenkins(client, headers, pid)
        body = client.get(f"/projects/{pid}/ci-setup", headers=headers).json()
    finally:
        get_settings.cache_clear()
    snippet = body["snippet"]

    assert body["taskType"] == SECURITY_ANALYSIS and body["task"] == "security"
    assert body["imageRef"] == "registry/modelmatch-agent-security:1.1.0"
    assert "registry/modelmatch-agent-security:1.1.0\n" in snippet  # the image, no args
    assert "modelmatch-agent:1.1.0" not in snippet                    # not the review image
    assert "stage('Driftplain Security Analysis')" in snippet
    assert f'-v "$PWD:{SECURITY_WORKSPACE}:ro"' in snippet
    for flag in _SECURITY_SANDBOX:
        assert flag in snippet
    assert AGENT_DIFF_ARG not in snippet and "git diff" not in snippet
    assert f"-e MODELMATCH_PROJECT_ID={pid} \\" in snippet
    assert "-e MODELMATCH_CI_TOKEN \\" in snippet
    assert "-e MODELMATCH_POST_RESULT=true \\" in snippet
    assert "-e BUILD_TAG \\" in snippet
    assert "DEEPSEEK_API_KEY = credentials('modelmatch-model-api-key')" in snippet
    assert "-e DEEPSEEK_API_KEY \\" in snippet
    # the review caps would abort a whole-repo scan: the image's own defaults apply
    assert "AGENT_TOKEN_CEILING" not in snippet and "AGENT_MAX_TOKENS" not in snippet
    for gone in ("curl", "jq ", "X-CI-Token", "LLM_CLIENT=", "AGENT_MODEL="):
        assert gone not in snippet, gone
    assert "REFUSED" in snippet and "exit $AGENT_RC" in snippet


def test_review_project_uses_the_review_image(client, db_session, monkeypatch):
    monkeypatch.setenv("AGENT_IMAGE", "registry/modelmatch-agent:1.1.0")
    monkeypatch.setenv("AGENT_SECURITY_IMAGE", "registry/modelmatch-agent-security:1.1.0")
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        load_seed(db_session)
        headers, _ = _register(client, db_session, "ci_rev_img@example.com")
        pid = _make_project(client, headers)
        _connect_jenkins(client, headers, pid)
        body = client.get(f"/projects/{pid}/ci-setup", headers=headers).json()
    finally:
        get_settings.cache_clear()
    assert body["imageRef"] == "registry/modelmatch-agent:1.1.0"
    assert "registry/modelmatch-agent:1.1.0 --diff pr.diff" in body["snippet"]
    assert "modelmatch-agent-security" not in body["snippet"]
    assert "stage('Driftplain AI Review')" in body["snippet"]


def test_security_snippet_bedrock_variant_mounts_aws_profile():
    snippet = build_security_snippet(
        api_url="http://backend",
        project_id=3,
        image_ref="modelmatch-agent-security:latest",
        aws_region="ap-south-1",
        auth_mode="aws_iam",
    )
    assert '-v "$PWD:/workspace:ro"' in snippet
    assert "AWS_DEFAULT_REGION=ap-south-1" in snippet
    assert '-v "$HOME/.aws:/home/appuser/.aws:ro"' in snippet
    assert "modelmatch-model-api-key" not in snippet


def test_api_key_runtime_config_without_env_var_is_a_config_error():
    with pytest.raises(ValueError):
        build_security_snippet(
            api_url="http://backend", project_id=3, image_ref="x",
            aws_region="ap-south-1", auth_mode="api_key", credential_env_var=None,
        )


# --- E20: additive ingest fields (cwe, cacheReadTokens) + the run's task ----------

def test_ingest_persists_cwe_and_cache_read_tokens(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ci_cwe@example.com")
    pid = _make_security_project(client, headers)
    token = _mint_token(client, headers, pid)

    body = _agent_result("jenkins-sec-1")
    body["model"] = "deepseek-v4-flash"
    body["gate"] = "fail"
    body["gateReason"] = "1 finding(s) at blocking severity (critical)"
    body["cacheReadTokens"] = 73_856
    body["findings"] = [
        {"severity": "critical", "category": "security", "file": "app.py", "line": 41,
         "message": "Jinja2 template rendered from user input",
         "cwe": "CWE-1336: Server-Side Template Injection"},
        {"severity": "low", "category": "security", "file": "config.py", "line": 3,
         "message": "hard-coded secret", "cwe": "  CWE-798: Use of Hard-coded Credentials  "},
        {"severity": "low", "category": "style", "file": "util.py", "line": 9,
         "message": "no cwe on this one", "cwe": None},
    ]
    resp = client.post(f"/projects/{pid}/ci-runs", json=body, headers={"X-CI-Token": token})
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["task"] == SECURITY_ANALYSIS
    assert out["cacheReadTokens"] == 73_856
    assert out["gate"] == "fail"

    run = db_session.scalar(select(CiRun).where(CiRun.project_id == pid))
    assert run.task == SECURITY_ANALYSIS
    assert run.cache_read_tokens == 73_856
    # cache reads are STORED, not priced: the cost is tokens_in/out only
    assert run.actual_cost is not None and run.tokens_in == 1200
    cwes = [f.cwe for f in db_session.scalars(
        select(CiFinding).where(CiFinding.ci_run_id == run.id).order_by(CiFinding.id)
    ).all()]
    assert cwes == [
        "CWE-1336: Server-Side Template Injection",
        "CWE-798: Use of Hard-coded Credentials",  # trimmed
        None,
    ]

    # …and the dashboard shows them: the run row lists the distinct CWE ids, the
    # drill-in carries the full cwe per finding, the envelope names the task.
    sav = client.get(f"/projects/{pid}/savings", headers=headers).json()
    assert sav["taskType"] == SECURITY_ANALYSIS
    assert sav["runs"][0]["cwes"] == ["CWE-1336", "CWE-798"]
    assert sav["runs"][0]["gate"] == "fail"
    drill = client.get(f"/projects/{pid}/runs/{run.id}/findings", headers=headers).json()
    assert [f["cwe"] for f in drill["findings"]] == [
        "CWE-1336: Server-Side Template Injection",
        "CWE-798: Use of Hard-coded Credentials",
        None,
    ]


def test_ingest_v1_payload_still_validates_and_review_runs_have_no_cwes(client, db_session):
    """A v1 agent sends neither cwe nor cacheReadTokens — both optional, nothing breaks."""
    load_seed(db_session)
    pid, token = _project_with_token(client, db_session, "ci_v1@example.com")
    body = _agent_result("v1-build")
    assert "cacheReadTokens" not in body and all("cwe" not in f for f in body["findings"])
    resp = client.post(f"/projects/{pid}/ci-runs", json=body, headers={"X-CI-Token": token})
    assert resp.status_code == 201
    assert resp.json()["cacheReadTokens"] is None
    assert resp.json()["task"] == CI_REVIEW
    run = db_session.scalar(select(CiRun).where(CiRun.project_id == pid))
    assert all(f.cwe is None for f in db_session.scalars(
        select(CiFinding).where(CiFinding.ci_run_id == run.id)
    ).all())


def test_ingest_unknown_field_is_still_422(client, db_session):
    """extra=forbid stays: a raw diff (or any unexpected key) is rejected, even now
    that two optional keys were added."""
    load_seed(db_session)
    pid, token = _project_with_token(client, db_session, "ci_extra@example.com")
    body = {**_agent_result(), "diff": "diff --git a/x b/x"}
    assert client.post(f"/projects/{pid}/ci-runs", json=body, headers={"X-CI-Token": token}).status_code == 422
    body = {**_agent_result(), "cacheReadTokens": -1}
    assert client.post(f"/projects/{pid}/ci-runs", json=body, headers={"X-CI-Token": token}).status_code == 422
    body = _agent_result()
    body["findings"][0]["cwe"] = "C" * (MAX_CWE_LEN + 1)
    assert client.post(f"/projects/{pid}/ci-runs", json=body, headers={"X-CI-Token": token}).status_code == 422


def test_ingest_accepts_multibranch_build_tag_with_percent(client, db_session):
    """June bug 6: a multibranch BUILD_TAG encodes a branch slash as %2F."""
    load_seed(db_session)
    pid, token = _project_with_token(client, db_session, "ci_pct@example.com")
    body = _agent_result("jenkins-modelmatch-demo-review-feature%2Fx-12")
    resp = client.post(f"/projects/{pid}/ci-runs", json=body, headers={"X-CI-Token": token})
    assert resp.status_code == 201
    assert resp.json()["jenkinsBuildId"] == "jenkins-modelmatch-demo-review-feature%2Fx-12"
    body = _agent_result("has space")
    assert client.post(f"/projects/{pid}/ci-runs", json=body, headers={"X-CI-Token": token}).status_code == 422


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
