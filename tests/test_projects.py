"""S8 project tests: create from a pick, list/get, owner-scoping + error paths.

A project is built from a real recommendation option (owned by the caller) plus a
baseline model. Owner-scoping is the point: you can't build from another user's
option, and you can't read another user's project.
"""

from sqlalchemy import func, select

from app.catalog.seed import load_seed
from app.models import (
    AgentRuntimeConfig,
    ChatMessage,
    CiFinding,
    CiRun,
    FindingFeedback,
    JenkinsConnection,
    LlmCall,
    Model,
    ProactiveAlert,
    Project,
    RecommendationOption,
    RequirementsProfile,
    RetrievalTrace,
    User,
)


def _runtime_config_for_option(db_session, option_id: int) -> AgentRuntimeConfig | None:
    option = db_session.get(RecommendationOption, option_id)
    assert option is not None
    return db_session.scalar(
        select(AgentRuntimeConfig).where(AgentRuntimeConfig.model_id == option.model_id)
    )


def _set_runtime_config_enabled(db_session, option_id: int, enabled: bool) -> None:
    config = _runtime_config_for_option(db_session, option_id)
    if config is None:
        option = db_session.get(RecommendationOption, option_id)
        config = AgentRuntimeConfig(
            model_id=option.model_id,
            provider="anthropic",
            provider_model_id="disabled-test-model",
            auth_mode="api_key",
            credential_env_var="ANTHROPIC_API_KEY",
            enabled=enabled,
        )
        db_session.add(config)
    else:
        config.enabled = enabled
    db_session.commit()


def _assert_runtime_config_422(resp) -> None:
    assert resp.status_code == 422
    detail = str(resp.json().get("detail", "")).lower()
    assert "runtime" in detail and "config" in detail
    assert "enabled" in detail


def _register(client, db_session, email: str) -> tuple[dict[str, str], int]:
    creds = {"email": email, "password": "correct horse battery"}
    client.post("/auth/register", json=creds)
    token = client.post("/auth/login", json=creds).json()["accessToken"]
    user_id = db_session.scalar(select(User.id).where(User.email == email))
    return {"Authorization": f"Bearer {token}"}, user_id


def _make_pick(client, headers) -> dict:
    """Create a recommendation; return its top option + baseline (the pick)."""
    body = client.post(
        "/recommendations",
        json={"taskTypes": ["ci_review"], "budgetSensitivity": "high"},
        headers=headers,
    ).json()
    top = body["shortlist"][0]
    return {
        "selected_option_id": top["recommendationOptionId"],
        "selected_model": top["model"],
        "baseline_model_id": body["baseline"]["modelId"],
        "baseline_model": body["baseline"]["model"],
    }


def test_create_project_persists_and_returns_enriched(client, db_session):
    load_seed(db_session)
    headers, user_id = _register(client, db_session, "p_create@example.com")
    pick = _make_pick(client, headers)

    resp = client.post(
        "/projects",
        json={
            "name": "my-repo CI",
            "selectedOptionId": pick["selected_option_id"],
            "baselineModelId": pick["baseline_model_id"],
        },
        headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "my-repo CI"
    assert body["userId"] == user_id
    assert body["selectedOptionId"] == pick["selected_option_id"]
    # enriched names resolved from the referenced rows
    assert body["selectedOptionModel"] == pick["selected_model"]
    assert body["baselineModel"] == pick["baseline_model"]  # "Claude Sonnet 4.5"
    assert body["baselineVendor"] == "Anthropic"

    # persisted + owner-scoped
    proj = db_session.get(Project, body["id"])
    assert proj is not None and proj.user_id == user_id


def test_list_projects_is_owner_scoped(client, db_session):
    load_seed(db_session)
    headers_a, _ = _register(client, db_session, "p_list_a@example.com")
    headers_b, _ = _register(client, db_session, "p_list_b@example.com")
    pick_a = _make_pick(client, headers_a)
    pick_b = _make_pick(client, headers_b)

    for name in ("a1", "a2"):
        client.post("/projects", json={
            "name": name,
            "selectedOptionId": pick_a["selected_option_id"],
            "baselineModelId": pick_a["baseline_model_id"],
        }, headers=headers_a)
    client.post("/projects", json={
        "name": "b1",
        "selectedOptionId": pick_b["selected_option_id"],
        "baselineModelId": pick_b["baseline_model_id"],
    }, headers=headers_b)

    a_projects = client.get("/projects", headers=headers_a).json()
    assert {p["name"] for p in a_projects} == {"a1", "a2"}  # B's "b1" not visible


def test_get_by_id_owner_scoped(client, db_session):
    load_seed(db_session)
    headers_a, _ = _register(client, db_session, "p_get_a@example.com")
    headers_b, _ = _register(client, db_session, "p_get_b@example.com")
    pick_a = _make_pick(client, headers_a)

    pid = client.post("/projects", json={
        "name": "secret",
        "selectedOptionId": pick_a["selected_option_id"],
        "baselineModelId": pick_a["baseline_model_id"],
    }, headers=headers_a).json()["id"]

    assert client.get(f"/projects/{pid}", headers=headers_a).status_code == 200
    assert client.get(f"/projects/{pid}", headers=headers_b).status_code == 403  # not yours


def test_get_nonexistent_project_404(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "p_404@example.com")
    assert client.get("/projects/999999", headers=headers).status_code == 404


def test_create_with_unknown_option_404(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "p_opt404@example.com")
    pick = _make_pick(client, headers)
    resp = client.post("/projects", json={
        "name": "x",
        "selectedOptionId": 999999,
        "baselineModelId": pick["baseline_model_id"],
    }, headers=headers)
    assert resp.status_code == 404


def test_create_with_another_users_option_403(client, db_session):
    load_seed(db_session)
    headers_a, _ = _register(client, db_session, "p_own_a@example.com")
    headers_b, _ = _register(client, db_session, "p_own_b@example.com")
    pick_a = _make_pick(client, headers_a)  # option owned by A
    pick_b = _make_pick(client, headers_b)

    # B tries to build a project from A's recommendation option
    resp = client.post("/projects", json={
        "name": "stolen",
        "selectedOptionId": pick_a["selected_option_id"],
        "baselineModelId": pick_b["baseline_model_id"],
    }, headers=headers_b)
    assert resp.status_code == 403


def test_create_with_unknown_baseline_model_404(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "p_base404@example.com")
    pick = _make_pick(client, headers)
    resp = client.post("/projects", json={
        "name": "x",
        "selectedOptionId": pick["selected_option_id"],
        "baselineModelId": 999999,
    }, headers=headers)
    assert resp.status_code == 404


def test_create_rejects_selected_option_without_enabled_runtime_config(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "p_runtime_create@example.com")
    pick = _make_pick(client, headers)
    _set_runtime_config_enabled(db_session, pick["selected_option_id"], False)

    resp = client.post("/projects", json={
        "name": "no runtime",
        "selectedOptionId": pick["selected_option_id"],
        "baselineModelId": pick["baseline_model_id"],
    }, headers=headers)

    _assert_runtime_config_422(resp)
    assert db_session.scalar(
        select(func.count()).select_from(Project).where(Project.name == "no runtime")
    ) == 0


def test_create_with_blank_name_422(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "p_blank@example.com")
    pick = _make_pick(client, headers)
    resp = client.post("/projects", json={
        "name": "",
        "selectedOptionId": pick["selected_option_id"],
        "baselineModelId": pick["baseline_model_id"],
    }, headers=headers)
    assert resp.status_code == 422


def test_create_with_overlong_name_422(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "p_long@example.com")
    pick = _make_pick(client, headers)
    resp = client.post("/projects", json={
        "name": "x" * 201,
        "selectedOptionId": pick["selected_option_id"],
        "baselineModelId": pick["baseline_model_id"],
    }, headers=headers)
    assert resp.status_code == 422


def test_projects_require_auth(client):
    assert client.get("/projects").status_code == 401
    assert client.post("/projects", json={
        "name": "x", "selectedOptionId": 1, "baselineModelId": 1,
    }).status_code == 401


# --- S15d helpers -------------------------------------------------------------

def _create_project(client, headers, pick, name="proj") -> dict:
    return client.post("/projects", json={
        "name": name,
        "selectedOptionId": pick["selected_option_id"],
        "baselineModelId": pick["baseline_model_id"],
    }, headers=headers).json()


# --- S15d: setupComplete ------------------------------------------------------

def test_setup_complete_false_until_ci_token(client, db_session):
    """A fresh project (no Jenkins connection, or one without a minted CI token) is
    setup-incomplete; it flips true only once the token hash exists (post /ci-setup)."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "p_setup@example.com")
    pick = _make_pick(client, headers)
    pid = _create_project(client, headers, pick)["id"]

    # no connection yet
    assert client.get(f"/projects/{pid}", headers=headers).json()["setupComplete"] is False

    # metadata-only connection (no token) → still incomplete
    client.put(f"/projects/{pid}/jenkins", json={
        "baseUrl": "http://jenkins.local", "jobName": "acme/main",
    }, headers=headers)
    assert client.get(f"/projects/{pid}", headers=headers).json()["setupComplete"] is False

    # mint the CI token (what /ci-setup does) → complete
    conn = db_session.scalar(
        select(JenkinsConnection).where(JenkinsConnection.project_id == pid)
    )
    conn.ci_token_hash = "deadbeef"
    db_session.commit()
    assert client.get(f"/projects/{pid}", headers=headers).json()["setupComplete"] is True


# --- S15d: PATCH (rename + re-pick) -------------------------------------------

def test_patch_repick_updates_selected_and_baseline(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "p_patch@example.com")
    pick = _make_pick(client, headers)
    pid = _create_project(client, headers, pick, name="before")["id"]

    # a fresh recommendation gives a new (caller-owned) option to re-pick
    pick2 = _make_pick(client, headers)
    other_baseline = db_session.scalar(
        select(Model.id).where(Model.id != pick["baseline_model_id"]).limit(1)
    )

    resp = client.patch(f"/projects/{pid}", json={
        "name": "after",
        "selectedOptionId": pick2["selected_option_id"],
        "baselineModelId": other_baseline,
    }, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "after"
    assert body["selectedOptionId"] == pick2["selected_option_id"]
    assert body["baselineModelId"] == other_baseline

    # persisted
    db_session.expire_all()
    proj = db_session.get(Project, pid)
    assert proj.selected_option_id == pick2["selected_option_id"]
    assert proj.baseline_model_id == other_baseline
    assert proj.name == "after"


def test_patch_name_only_leaves_pick(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "p_patchname@example.com")
    pick = _make_pick(client, headers)
    pid = _create_project(client, headers, pick, name="orig")["id"]

    resp = client.patch(f"/projects/{pid}", json={"name": "renamed"}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "renamed"
    assert body["selectedOptionId"] == pick["selected_option_id"]  # untouched
    assert body["baselineModelId"] == pick["baseline_model_id"]


def test_patch_empty_body_is_noop(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "p_patchnoop@example.com")
    pick = _make_pick(client, headers)
    pid = _create_project(client, headers, pick, name="keep")["id"]

    resp = client.patch(f"/projects/{pid}", json={}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "keep"


def test_patch_blank_name_422(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "p_patchblank@example.com")
    pick = _make_pick(client, headers)
    pid = _create_project(client, headers, pick)["id"]
    assert client.patch(f"/projects/{pid}", json={"name": ""}, headers=headers).status_code == 422


def test_patch_owner_scoped_403(client, db_session):
    load_seed(db_session)
    headers_a, _ = _register(client, db_session, "p_patch_a@example.com")
    headers_b, _ = _register(client, db_session, "p_patch_b@example.com")
    pick_a = _make_pick(client, headers_a)
    pid = _create_project(client, headers_a, pick_a)["id"]
    # B cannot rename A's project
    assert client.patch(f"/projects/{pid}", json={"name": "hijack"}, headers=headers_b).status_code == 403


def test_patch_repick_to_another_users_option_403(client, db_session):
    load_seed(db_session)
    headers_a, _ = _register(client, db_session, "p_patch_opt_a@example.com")
    headers_b, _ = _register(client, db_session, "p_patch_opt_b@example.com")
    pick_a = _make_pick(client, headers_a)
    pick_b = _make_pick(client, headers_b)  # option owned by B
    pid = _create_project(client, headers_a, pick_a)["id"]
    # A tries to re-pick onto B's option
    resp = client.patch(f"/projects/{pid}", json={
        "selectedOptionId": pick_b["selected_option_id"],
    }, headers=headers_a)
    assert resp.status_code == 403


def test_patch_unknown_option_404(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "p_patch_opt404@example.com")
    pick = _make_pick(client, headers)
    pid = _create_project(client, headers, pick)["id"]
    resp = client.patch(f"/projects/{pid}", json={"selectedOptionId": 999999}, headers=headers)
    assert resp.status_code == 404


def test_patch_unknown_baseline_404(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "p_patch_base404@example.com")
    pick = _make_pick(client, headers)
    pid = _create_project(client, headers, pick)["id"]
    resp = client.patch(f"/projects/{pid}", json={"baselineModelId": 999999}, headers=headers)
    assert resp.status_code == 404


def test_patch_repick_rejects_option_without_enabled_runtime_config(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "p_patch_runtime@example.com")
    pick = _make_pick(client, headers)
    pid = _create_project(client, headers, pick)["id"]
    replacement = _make_pick(client, headers)
    _set_runtime_config_enabled(db_session, replacement["selected_option_id"], False)

    resp = client.patch(
        f"/projects/{pid}",
        json={"selectedOptionId": replacement["selected_option_id"]},
        headers=headers,
    )

    _assert_runtime_config_422(resp)
    db_session.expire_all()
    assert db_session.get(Project, pid).selected_option_id == pick["selected_option_id"]


def test_patch_nonexistent_404(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "p_patch404@example.com")
    assert client.patch("/projects/999999", json={"name": "x"}, headers=headers).status_code == 404


def test_patch_requires_auth_401(client):
    assert client.patch("/projects/1", json={"name": "x"}).status_code == 401


# --- S15d: DELETE (cascade + owner-scope) -------------------------------------

def test_delete_removes_project_and_cascades(client, db_session):
    """DELETE removes the project AND its whole subtree via the DB FK cascade —
    Jenkins connection, ci_run→ci_finding→finding_feedback, llm_call, chat_message→
    retrieval_trace, proactive_alert — with no FK violation."""
    load_seed(db_session)
    headers, user_id = _register(client, db_session, "p_del@example.com")
    pick = _make_pick(client, headers)
    pid = _create_project(client, headers, pick)["id"]

    # build the full child subtree directly
    conn = JenkinsConnection(
        project_id=pid, base_url="http://j", job_name="j",
        ci_token_hash="tok", status="configured",
    )
    run = CiRun(project_id=pid, jenkins_build_id="1", task="code_review")
    db_session.add_all([conn, run])
    db_session.flush()
    finding = CiFinding(ci_run_id=run.id, category="security", severity="high", message="m")
    llm = LlmCall(ci_run_id=run.id, purpose="agent", status="ok")
    msg = ChatMessage(project_id=pid, role="user", text="hi")
    alert = ProactiveAlert(project_id=pid, kind="upgrade", status="open")
    db_session.add_all([finding, llm, msg, alert])
    db_session.flush()
    fb = FindingFeedback(ci_finding_id=finding.id, verdict="accept", user_id=user_id)
    trace = RetrievalTrace(chat_message_id=msg.id, kind="savings", ref="r")
    db_session.add_all([fb, trace])
    db_session.commit()
    run_id, finding_id, llm_id = run.id, finding.id, llm.id
    fb_id, msg_id, trace_id, alert_id = fb.id, msg.id, trace.id, alert.id

    resp = client.delete(f"/projects/{pid}", headers=headers)
    assert resp.status_code == 204

    db_session.expire_all()
    assert db_session.get(Project, pid) is None
    assert db_session.scalar(
        select(func.count()).select_from(JenkinsConnection).where(JenkinsConnection.project_id == pid)
    ) == 0
    assert db_session.get(CiRun, run_id) is None
    assert db_session.get(CiFinding, finding_id) is None
    assert db_session.get(FindingFeedback, fb_id) is None
    assert db_session.get(LlmCall, llm_id) is None
    assert db_session.get(ChatMessage, msg_id) is None
    assert db_session.get(RetrievalTrace, trace_id) is None
    assert db_session.get(ProactiveAlert, alert_id) is None


def test_delete_only_targets_its_own_subtree(client, db_session):
    """Deleting one project leaves a sibling project (same owner) untouched."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "p_del_sibling@example.com")
    pick = _make_pick(client, headers)
    keep_id = _create_project(client, headers, pick, name="keep")["id"]
    drop_id = _create_project(client, headers, pick, name="drop")["id"]

    assert client.delete(f"/projects/{drop_id}", headers=headers).status_code == 204
    assert client.get(f"/projects/{keep_id}", headers=headers).status_code == 200


def test_delete_keeps_recommendation_history(client, db_session):
    """Cascade scope is deliberately the project's *subtree*, NOT its recommender
    history. The selected recommendation_option (and its requirements_profile) are
    *parents* the project points at — shared, reusable picks — so deleting a project
    leaves them intact. (Documents the S15d backlog's ambiguous "recommendation rows".)"""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "p_del_history@example.com")
    pick = _make_pick(client, headers)
    pid = _create_project(client, headers, pick)["id"]

    option_id = pick["selected_option_id"]
    profile_id = db_session.scalar(
        select(RecommendationOption.profile_id).where(RecommendationOption.id == option_id)
    )

    assert client.delete(f"/projects/{pid}", headers=headers).status_code == 204

    db_session.expire_all()
    assert db_session.get(Project, pid) is None
    assert db_session.get(RecommendationOption, option_id) is not None  # parent survives
    assert db_session.get(RequirementsProfile, profile_id) is not None


def test_delete_owner_scoped_403(client, db_session):
    load_seed(db_session)
    headers_a, _ = _register(client, db_session, "p_del_a@example.com")
    headers_b, _ = _register(client, db_session, "p_del_b@example.com")
    pick_a = _make_pick(client, headers_a)
    pid = _create_project(client, headers_a, pick_a)["id"]
    # B cannot delete A's project; it survives
    assert client.delete(f"/projects/{pid}", headers=headers_b).status_code == 403
    assert client.get(f"/projects/{pid}", headers=headers_a).status_code == 200


def test_delete_nonexistent_404(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "p_del404@example.com")
    assert client.delete("/projects/999999", headers=headers).status_code == 404


def test_delete_requires_auth_401(client):
    assert client.delete("/projects/1").status_code == 401


# --- E20: the project's task + review preferences --------------------------------

def _security_recommendation(client, headers) -> dict:
    return client.post(
        "/recommendations",
        json={"taskTypes": ["security_analysis"], "budgetSensitivity": "high"},
        headers=headers,
    ).json()


def _recommendation(client, headers) -> dict:
    return client.post(
        "/recommendations",
        json={"taskTypes": ["ci_review"], "budgetSensitivity": "high"},
        headers=headers,
    ).json()


def _create(client, headers) -> int:
    return _create_project(client, headers, _make_pick(client, headers))["id"]


def test_create_derives_task_type_from_the_option_when_omitted(client, db_session):
    """An older client sends no taskType: the project takes the task its selected
    option was ranked on (ci_review here), and has no preferences."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "task_derive@example.com")
    rec = _recommendation(client, headers)
    out = client.post(
        "/projects",
        json={
            "name": "p",
            "selectedOptionId": rec["shortlist"][0]["recommendationOptionId"],
            "baselineModelId": rec["baseline"]["modelId"],
        },
        headers=headers,
    ).json()
    assert out["taskType"] == "ci_review"
    assert out["reviewPreferences"] is None
    assert db_session.get(Project, out["id"]).task_type == "ci_review"


def test_create_security_project_states_its_task(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "task_sec@example.com")
    rec = _security_recommendation(client, headers)
    resp = client.post(
        "/projects",
        json={
            "name": "sec",
            "selectedOptionId": rec["shortlist"][0]["recommendationOptionId"],
            "baselineModelId": rec["baseline"]["modelId"],
            "taskType": "security_analysis",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["taskType"] == "security_analysis"
    assert resp.json()["selectedOptionModel"] == "DeepSeek V4 Flash"


def test_create_rejects_task_type_that_contradicts_the_option_422(client, db_session):
    """An option ranked on RealVuln cannot back a review project (and vice versa)."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "task_mismatch@example.com")
    rec = _security_recommendation(client, headers)
    resp = client.post(
        "/projects",
        json={
            "name": "p",
            "selectedOptionId": rec["shortlist"][0]["recommendationOptionId"],
            "baselineModelId": rec["baseline"]["modelId"],
            "taskType": "ci_review",
        },
        headers=headers,
    )
    assert resp.status_code == 422
    assert "security_analysis" in resp.json()["detail"]


def test_create_rejects_unknown_task_type_422(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "task_unknown@example.com")
    rec = _recommendation(client, headers)
    resp = client.post(
        "/projects",
        json={
            "name": "p",
            "selectedOptionId": rec["shortlist"][0]["recommendationOptionId"],
            "baselineModelId": rec["baseline"]["modelId"],
            "taskType": "agentic_coding",
        },
        headers=headers,
    )
    assert resp.status_code == 422


def test_create_stores_bounded_review_preferences(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "prefs@example.com")
    rec = _recommendation(client, headers)
    base = {
        "name": "p",
        "selectedOptionId": rec["shortlist"][0]["recommendationOptionId"],
        "baselineModelId": rec["baseline"]["modelId"],
        "taskType": "ci_review",
    }
    out = client.post(
        "/projects",
        json={**base, "reviewPreferences": "  Flag any use of eval().  "},
        headers=headers,
    ).json()
    assert out["reviewPreferences"] == "Flag any use of eval()."  # trimmed
    # whitespace-only → none
    out2 = client.post("/projects", json={**base, "reviewPreferences": "   "}, headers=headers).json()
    assert out2["reviewPreferences"] is None
    # over the contract's bound → 422
    resp = client.post("/projects", json={**base, "reviewPreferences": "x" * 2001}, headers=headers)
    assert resp.status_code == 422


def test_patch_review_preferences_sets_and_clears(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "prefs_patch@example.com")
    pid = _create(client, headers)
    out = client.patch(
        f"/projects/{pid}", json={"reviewPreferences": "Ignore import ordering."}, headers=headers
    ).json()
    assert out["reviewPreferences"] == "Ignore import ordering."
    # a name-only PATCH leaves them alone
    out = client.patch(f"/projects/{pid}", json={"name": "renamed"}, headers=headers).json()
    assert out["reviewPreferences"] == "Ignore import ordering."
    # explicit null clears
    out = client.patch(f"/projects/{pid}", json={"reviewPreferences": None}, headers=headers).json()
    assert out["reviewPreferences"] is None
    assert db_session.get(Project, pid).review_preferences is None


def test_patch_task_type_must_match_the_current_option(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "task_patch@example.com")
    pid = _create(client, headers)  # a ci_review project
    assert client.patch(f"/projects/{pid}", json={"taskType": "ci_review"}, headers=headers).status_code == 200
    resp = client.patch(f"/projects/{pid}", json={"taskType": "security_analysis"}, headers=headers)
    assert resp.status_code == 422
    assert db_session.get(Project, pid).task_type == "ci_review"


def test_list_and_get_expose_task_type(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "task_list@example.com")
    pid = _create(client, headers)
    assert client.get("/projects", headers=headers).json()[0]["taskType"] == "ci_review"
    assert client.get(f"/projects/{pid}", headers=headers).json()["taskType"] == "ci_review"
