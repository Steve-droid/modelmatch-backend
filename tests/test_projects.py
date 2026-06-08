"""S8 project tests: create from a pick, list/get, owner-scoping + error paths.

A project is built from a real recommendation option (owned by the caller) plus a
baseline model. Owner-scoping is the point: you can't build from another user's
option, and you can't read another user's project.
"""

from sqlalchemy import select

from app.catalog.seed import load_seed
from app.models import Project, User


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
