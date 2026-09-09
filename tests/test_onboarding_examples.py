"""New-account examples use real persistence and catalog, with zero live models."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import google, service
from app.catalog.seed import load_seed
from app.config import get_settings
from app.demo.onboarding import EXAMPLE_PROJECTS, provision_examples
from app.models import (User, Project, CiRun, CiFinding, FindingFeedback, JenkinsConnection,
                        RequirementsProfile, RecommendationOption, LlmUsage)
from tests.test_google_auth import google_env, sign_in


@pytest.fixture
def examples(db_session, monkeypatch):
    load_seed(db_session)
    monkeypatch.setenv("SEED_NEW_USER_EXAMPLES", "true")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def register(client, email):
    response = client.post("/auth/register", json={"email": email, "password": "example-password"})
    assert response.status_code == 201, response.text
    token = client.post("/auth/login", json={"email": email, "password": "example-password"}).json()["accessToken"]
    return response.json()["id"], {"Authorization": f"Bearer {token}"}


def assert_examples(client, db, uid, headers):
    projects = client.get("/projects", headers=headers).json()
    assert {p["name"] for p in projects} == {name for _, name, _ in EXAMPLE_PROJECTS}
    assert len(projects) == 2
    for p in projects:
        assert p["userId"] == uid and p["isExample"] is True
        assert p["setupComplete"] is False  # no fabricated Jenkins connection/token
        expected = 30 if p["taskType"] == "ci_review" else 20
        savings = client.get(f'/projects/{p["id"]}/savings?range=all', headers=headers)
        assert savings.status_code == 200
        kpis = savings.json()["kpis"]
        assert kpis["runsCount"] == expected
        assert kpis["bankedRuns"] > 0 and kpis["qualityRiskRuns"] > 0 and kpis["unratedRuns"] > 0
        assert float(kpis["cumulativeSaved"]) > 0
        assert db.scalar(select(func.count()).select_from(CiRun).where(CiRun.project_id == p["id"])) == expected
        assert client.get(f'/projects/{p["id"]}/chat', headers=headers).status_code == 403
    assert db.scalar(select(func.count()).select_from(JenkinsConnection)) == 0
    assert db.scalar(select(func.count()).select_from(LlmUsage)) == 0
    return projects


def test_password_signup_has_two_owned_populated_examples(client, db_session, examples):
    uid, headers = register(client, "first@example.com")
    assert_examples(client, db_session, uid, headers)
    assert not db_session.get(User, uid).is_operator


def test_google_signup_and_returning_login_are_idempotent(client, db_session, examples, google_env):
    response, _ = sign_in(client, google_env)
    assert response.status_code == 200
    headers = {"Authorization": "Bearer " + response.json()["accessToken"]}
    uid = client.get("/auth/me", headers=headers).json()["id"]
    projects = assert_examples(client, db_session, uid, headers)
    assert sign_in(client, google_env)[0].status_code == 200
    assert client.get("/projects", headers=headers).json() == projects
    assert client.delete(f'/projects/{projects[0]["id"]}', headers=headers).status_code == 204
    assert sign_in(client, google_env)[0].status_code == 200
    assert len(client.get("/projects", headers=headers).json()) == 1


def test_users_have_separate_runs_findings_feedback_and_cannot_read_each_other(client, db_session, examples):
    u1, h1 = register(client, "first@example.com")
    u2, h2 = register(client, "second@example.com")
    p1 = assert_examples(client, db_session, u1, h1)
    p2 = assert_examples(client, db_session, u2, h2)
    ids1 = {p["id"] for p in p1};ids2 = {p["id"] for p in p2}
    assert ids1.isdisjoint(ids2)
    assert client.get(f'/projects/{p1[0]["id"]}/savings', headers=h2).status_code == 403
    assert client.delete(f'/projects/{p1[0]["id"]}', headers=h2).status_code == 403
    for uid, ids in [(u1, ids1), (u2, ids2)]:
        verdict_owners = set(db_session.scalars(select(FindingFeedback.user_id).join(
            CiFinding, CiFinding.id == FindingFeedback.ci_finding_id).join(CiRun).where(CiRun.project_id.in_(ids))))
        assert verdict_owners == {uid}


def test_examples_cannot_be_connected_or_converted_and_can_be_deleted(client, db_session, examples):
    uid, headers = register(client, "example@example.com")
    projects = assert_examples(client, db_session, uid, headers)
    pid = projects[0]["id"]
    assert client.get(f"/projects/{pid}/ci-setup", headers=headers).status_code == 409
    assert client.post(f"/projects/{pid}/ci-setup/rotate", headers=headers).status_code == 409
    assert client.put(f"/projects/{pid}/jenkins", headers=headers,
                      json={"baseUrl": "https://ci.example.com", "jobName": "job"}).status_code == 409
    assert client.patch(f"/projects/{pid}", headers=headers, json={"name": "real", "isExample": False}).status_code == 409
    assert client.post(f"/projects/{pid}/ci-runs", json={}, headers={"X-CI-Token": "forged"}).status_code == 401
    assert client.delete(f"/projects/{pid}", headers=headers).status_code == 204
    assert len(client.get("/projects", headers=headers).json()) == 1


@pytest.mark.parametrize("method", ["password", "google"])
def test_failure_on_second_project_rolls_back_entire_signup(client, db_session, examples, google_env, method):
    import app.demo.seed as seed
    original = seed.seed_runs
    def fail_second(*args, **kwargs):
        if kwargs.get("task") == "security_analysis":
            raise HTTPException(503, "fixture failure")
        return original(*args, **kwargs)
    examples.setattr(seed, "seed_runs", fail_second)
    response = (client.post("/auth/register", json={"email": "fail@example.com", "password": "pw"})
                if method == "password" else sign_in(client, google_env)[0])
    assert response.status_code == 503
    for model in (User, Project, CiRun, CiFinding, FindingFeedback, RequirementsProfile, RecommendationOption):
        assert db_session.scalar(select(func.count()).select_from(model)) == 0, model
    examples.setattr(seed, "seed_runs", original)
    uid, headers = register(client, "retry@example.com")
    assert_examples(client, db_session, uid, headers)


def test_existing_login_does_not_backfill_or_reseed(client, db_session, examples):
    examples.setenv("SEED_NEW_USER_EXAMPLES", "false");get_settings.cache_clear()
    uid, headers = register(client, "old@example.com")
    examples.setenv("SEED_NEW_USER_EXAMPLES", "true");get_settings.cache_clear()
    client.post("/auth/login", json={"email": "old@example.com", "password": "example-password"})
    assert client.get("/projects", headers=headers).json() == []
    assert db_session.get(User, uid).email == "old@example.com"


def test_internal_retry_keeps_existing_example_ids_and_runs(client, db_session, examples):
    uid, headers = register(client, "first@example.com")
    before = client.get("/projects", headers=headers).json()
    ids_before = list(db_session.scalars(select(CiRun.id).order_by(CiRun.id)))
    provision_examples(db_session, db_session.get(User, uid));db_session.commit()
    assert client.get("/projects", headers=headers).json() == before
    assert list(db_session.scalars(select(CiRun.id).order_by(CiRun.id))) == ids_before


def test_concurrent_mixed_signup_preserves_cap_and_complete_pairs(migrated_engine, db_session, examples):
    examples.setenv("MAX_REGISTERED_USERS", "2");get_settings.cache_clear()
    examples.setattr(google, "verify_credential", lambda credential, nonce: {"sub": credential, "email": credential+"@example.com"})
    barrier = Barrier(4)
    def signup(n):
        challenge = google.create_challenge().challenge
        barrier.wait()
        with Session(migrated_engine) as db:
            try:
                if n % 2:
                    google.authenticate_google(db, f"google{n}", challenge)
                else:
                    service.register_user(db, f"password{n}@example.com", "pw")
                return 201
            except HTTPException as e:
                return e.status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(signup, range(4)))
    assert sorted(statuses) == [201, 201, 409, 409]
    assert db_session.scalar(select(func.count()).select_from(User)) == 2
    assert db_session.scalar(select(func.count()).select_from(Project)) == 4
    assert db_session.scalar(select(func.count()).select_from(CiRun)) == 100
