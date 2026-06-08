"""S8 project tests: create from a pick, list/get, owner-scoping + error paths.

A project is built from a real recommendation option (owned by the caller) plus a
baseline model. Owner-scoping is the point: you can't build from another user's
option, and you can't read another user's project.
"""

from sqlalchemy import func, select

from app.catalog.seed import load_seed
from app.models import (
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
