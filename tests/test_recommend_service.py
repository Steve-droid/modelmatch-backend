"""S6 recommender service/endpoint tests against the seeded compose Postgres.

Exercises the full path: POST /recommendations → filter the seed catalog → rank in
the dominant comparability group → choose the configured baseline → persist the
profile + options + evidence (owner-scoped). Determinism is checked end-to-end.
"""

from sqlalchemy import func, select

from app.catalog.seed import load_seed
from app.models import (
    RecommendationEvidence,
    RecommendationOption,
    RequirementsProfile,
    User,
)


def _register(client, db_session, email: str) -> tuple[dict[str, str], int]:
    """Register+login a user; return (auth header, that user's DB id)."""
    creds = {"email": email, "password": "correct horse battery"}
    client.post("/auth/register", json=creds)
    token = client.post("/auth/login", json=creds).json()["accessToken"]
    user_id = db_session.scalar(select(User.id).where(User.email == email))
    return {"Authorization": f"Bearer {token}"}, user_id


def _auth_header(client) -> dict[str, str]:
    creds = {"email": "rec@example.com", "password": "correct horse battery"}
    client.post("/auth/register", json=creds)
    token = client.post("/auth/login", json=creds).json()["accessToken"]
    return {"Authorization": f"Bearer {token}"}


def test_high_sensitivity_picks_cheap_high_value_model(client, db_session):
    load_seed(db_session)
    headers = _auth_header(client)

    resp = client.post(
        "/recommendations",
        json={"taskTypes": ["agentic_coding"], "budgetSensitivity": "high"},
        headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()

    # ranked within the like-for-like group only
    assert body["comparabilityGroup"]["benchmark"] == "SWE-bench Verified"
    assert body["comparabilityGroup"]["metric"] == "pass@1_percent"

    # cost-leaning: a cheap, decent model wins — Gemini 2.5 Flash (50 @ $0.30)
    assert body["suggested"]["rank"] == 1
    assert body["suggested"]["model"] == "Gemini 2.5 Flash"

    # baseline = the configured model NAME, found in-group, with its model identity
    assert body["baseline"]["model"] == "Claude Sonnet 4.x"
    assert body["baseline"]["selection"] == "configured"
    assert float(body["baseline"]["costPerMtok"]) == 3.0
    assert body["baseline"]["vendor"] == "Anthropic"
    assert isinstance(body["baseline"]["modelId"], int)

    # shortlist of 3 (the env default); each item carries the persisted option id +
    # model identity S8 needs, plus its rank_score + evidence row
    assert len(body["shortlist"]) == 3
    for o in body["shortlist"]:
        assert isinstance(o["recommendationOptionId"], int)
        assert isinstance(o["modelId"], int)
        assert o["vendor"]
        assert "rankScore" in o and "benchmarkResultId" in o


def test_low_sensitivity_shifts_to_higher_quality_model(client, db_session):
    load_seed(db_session)
    headers = _auth_header(client)

    resp = client.post(
        "/recommendations",
        json={"taskTypes": ["agentic_coding"], "budgetSensitivity": "low"},
        headers=headers,
    )
    assert resp.status_code == 201
    # quality-leaning: the top-scoring model (GPT-5, 69.5) rises to #1
    assert resp.json()["suggested"]["model"] == "GPT-5"


def test_persists_profile_options_evidence(client, db_session):
    load_seed(db_session)
    headers, user_id = _register(client, db_session, "persist@example.com")

    body = client.post(
        "/recommendations",
        json={"taskTypes": ["agentic_coding"], "budgetSensitivity": "medium"},
        headers=headers,
    ).json()

    # exactly one profile, owned by the authenticated user (exact id, not just non-null)
    profiles = db_session.scalars(select(RequirementsProfile)).all()
    assert len(profiles) == 1
    assert profiles[0].id == body["profileId"]
    assert profiles[0].task_types == ["agentic_coding"]
    assert profiles[0].budget_sensitivity == "medium"
    assert profiles[0].user_id == user_id

    # one option per shortlist entry, each with a non-null rank_score
    options = db_session.scalars(
        select(RecommendationOption).where(
            RecommendationOption.profile_id == profiles[0].id
        )
    ).all()
    assert len(options) == 3
    assert all(o.rank_score is not None for o in options)
    # provenance: each option has at least one evidence row
    n_evidence = db_session.scalar(select(func.count()).select_from(RecommendationEvidence))
    assert n_evidence == 3


def test_returned_option_ids_exist_and_belong_to_the_profile(client, db_session):
    """The recommendationOptionIds in the response are real rows under this profile
    (the contract S8 relies on to create a project)."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "ids@example.com")

    body = client.post(
        "/recommendations",
        json={"taskTypes": ["agentic_coding"], "budgetSensitivity": "medium"},
        headers=headers,
    ).json()
    profile_id = body["profileId"]

    for o in body["shortlist"]:
        opt = db_session.get(RecommendationOption, o["recommendationOptionId"])
        assert opt is not None
        assert opt.profile_id == profile_id
        assert opt.model_id == o["modelId"]


def test_recommendation_is_owned_by_creating_user_not_another(client, db_session):
    load_seed(db_session)
    headers_a, user_a = _register(client, db_session, "alice@example.com")
    _, user_b = _register(client, db_session, "bob@example.com")
    assert user_a != user_b

    body = client.post(
        "/recommendations",
        json={"taskTypes": ["agentic_coding"], "budgetSensitivity": "medium"},
        headers=headers_a,  # created by Alice
    ).json()

    profile = db_session.get(RequirementsProfile, body["profileId"])
    assert profile.user_id == user_a  # owned by Alice, not Bob
    assert profile.user_id != user_b


def test_same_request_is_deterministic(client, db_session):
    load_seed(db_session)
    headers = _auth_header(client)
    payload = {"taskTypes": ["agentic_coding"], "budgetSensitivity": "high"}

    a = client.post("/recommendations", json=payload, headers=headers).json()
    b = client.post("/recommendations", json=payload, headers=headers).json()

    assert [o["model"] for o in a["shortlist"]] == [o["model"] for o in b["shortlist"]]
    assert [o["rankScore"] for o in a["shortlist"]] == [o["rankScore"] for o in b["shortlist"]]


def test_multiple_task_types_rank_within_dominant_group(client, db_session):
    load_seed(db_session)
    headers = _auth_header(client)

    body = client.post(
        "/recommendations",
        json={"taskTypes": ["agentic_coding", "long_context"], "budgetSensitivity": "medium"},
        headers=headers,
    ).json()
    # agentic_coding (7 rows) dominates long_context (2 rows); we rank within it and
    # never normalize a pass@1 row against an accuracy row.
    assert body["comparabilityGroup"]["metric"] == "pass@1_percent"


def test_no_matching_task_types_is_422(client, db_session):
    load_seed(db_session)
    headers = _auth_header(client)
    resp = client.post(
        "/recommendations",
        json={"taskTypes": ["does_not_exist"], "budgetSensitivity": "medium"},
        headers=headers,
    )
    assert resp.status_code == 422


def test_recommendation_requires_auth(client):
    resp = client.post(
        "/recommendations",
        json={"taskTypes": ["agentic_coding"], "budgetSensitivity": "medium"},
    )
    assert resp.status_code == 401
