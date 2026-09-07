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


def test_review_task_picks_the_cheap_high_value_model(client, db_session):
    """The product's headline pick: on the CI-review task the recommender chooses
    Claude Haiku 4.5 — 85.0 review score against the baseline's 87.1, at a third of
    the price — and reports the group it compared within."""
    load_seed(db_session)
    headers = _auth_header(client)

    resp = client.post(
        "/recommendations",
        json={"taskTypes": ["ci_review"], "budgetSensitivity": "high"},
        headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()

    # ranked within the like-for-like CI-review group only (CodeReviewBench)
    assert body["comparabilityGroup"]["benchmark"] == "CodeReviewBench"
    assert body["comparabilityGroup"]["metric"] == "review_score_percent"

    assert body["suggested"]["rank"] == 1
    assert body["suggested"]["model"] == "Claude Haiku 4.5"

    # baseline = the model configured FOR THIS TASK, found in-group, with its identity
    assert body["baseline"]["model"] == "Claude Sonnet 4.5"
    assert body["baseline"]["selection"] == "configured"
    assert float(body["baseline"]["costPerMtok"]) == 6.0  # blended (3*3 + 15)/4
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


def test_security_task_uses_its_own_benchmark_and_baseline(client, db_session):
    """P38c: a second task, measured by its own benchmark, with its own baseline.
    Selecting security_analysis must never rank a review score against an F3 score."""
    load_seed(db_session)
    headers = _auth_header(client)

    body = client.post(
        "/recommendations",
        json={"taskTypes": ["security_analysis"], "budgetSensitivity": "low"},
        headers=headers,
    ).json()

    assert body["comparabilityGroup"]["benchmark"] == "RealVuln"
    assert body["comparabilityGroup"]["metric"] == "f3_score"
    # the security task's own configured baseline, not the review task's Sonnet
    assert body["baseline"]["model"] == "Claude Opus 5"
    assert body["baseline"]["selection"] == "configured"


def test_budget_sensitivity_steers_the_pick(client, db_session):
    """The cost<->quality slider visibly moves the WINNER, not just the spread.

    Demonstrated on the security task, whose benchmark spans a wide price range: a
    quality-leaning setting buys the strongest scanner (Claude Opus 5, F3 67.7 at
    $5/$25), while a cost-leaning setting trades score for a far cheaper model. This
    is the demo's headline behaviour — the formula, not a model, makes the call."""
    load_seed(db_session)
    headers = _auth_header(client)

    def pick(sensitivity: str) -> dict:
        return client.post(
            "/recommendations",
            json={"taskTypes": ["security_analysis"], "budgetSensitivity": sensitivity},
            headers=headers,
        ).json()["suggested"]

    cost_leaning = pick("high")
    quality_leaning = pick("low")

    assert quality_leaning["model"] == "Claude Opus 5"
    # the winner actually changes across the slider, and changes in the right direction
    assert cost_leaning["model"] != quality_leaning["model"]
    assert float(cost_leaning["costPerMtok"]) < float(quality_leaning["costPerMtok"])


def test_persists_profile_options_evidence(client, db_session):
    load_seed(db_session)
    headers, user_id = _register(client, db_session, "persist@example.com")

    body = client.post(
        "/recommendations",
        json={"taskTypes": ["ci_review"], "budgetSensitivity": "medium"},
        headers=headers,
    ).json()

    # exactly one profile, owned by the authenticated user (exact id, not just non-null)
    profiles = db_session.scalars(select(RequirementsProfile)).all()
    assert len(profiles) == 1
    assert profiles[0].id == body["profileId"]
    assert profiles[0].task_types == ["ci_review"]
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
        json={"taskTypes": ["ci_review"], "budgetSensitivity": "medium"},
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
        json={"taskTypes": ["ci_review"], "budgetSensitivity": "medium"},
        headers=headers_a,  # created by Alice
    ).json()

    profile = db_session.get(RequirementsProfile, body["profileId"])
    assert profile.user_id == user_a  # owned by Alice, not Bob
    assert profile.user_id != user_b


def test_same_request_is_deterministic(client, db_session):
    load_seed(db_session)
    headers = _auth_header(client)
    payload = {"taskTypes": ["ci_review"], "budgetSensitivity": "high"}

    a = client.post("/recommendations", json=payload, headers=headers).json()
    b = client.post("/recommendations", json=payload, headers=headers).json()

    assert [o["model"] for o in a["shortlist"]] == [o["model"] for o in b["shortlist"]]
    assert [o["rankScore"] for o in a["shortlist"]] == [o["rankScore"] for o in b["shortlist"]]


def test_task_types_from_two_benchmarks_are_rejected_not_voted_on(client, db_session):
    """P38c: selecting task types measured by DIFFERENT benchmarks has no comparable
    ranking, so the API says so (422) instead of silently ranking whichever group had
    more rows. ci_review is measured by CodeReviewBench and agentic_coding by
    SWE-bench Verified — a review score and a pass@1 score share no scale."""
    load_seed(db_session)
    headers = _auth_header(client)

    resp = client.post(
        "/recommendations",
        json={"taskTypes": ["ci_review", "agentic_coding"], "budgetSensitivity": "medium"},
        headers=headers,
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "CodeReviewBench" in detail and "SWE-bench Verified" in detail


def test_one_task_type_ranks_inside_its_own_benchmark(client, db_session):
    """The flip side: each task type alone ranks cleanly within its single group."""
    load_seed(db_session)
    headers = _auth_header(client)

    body = client.post(
        "/recommendations",
        json={"taskTypes": ["agentic_coding"], "budgetSensitivity": "medium"},
        headers=headers,
    ).json()
    assert body["comparabilityGroup"]["benchmark"] == "SWE-bench Verified"
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


def test_pick_is_restricted_to_models_the_agent_can_run(client, db_session):
    """P38c: the recommendation must be deployable.

    RealVuln scores 16 scanners; the agent is known to drive three of them. Ranking
    the rest would hand the user a confident pick their pipeline cannot run — so the
    pick ranks only models with an enabled agent_runtime_config, and the response
    reports both counts so the narrowing is visible rather than silent. The excluded
    rows stay in the catalog and the chat. That row is a claim about what we SUPPORT,
    not about credentials we hold: the table has no user_id and credential_env_var
    stores an env var NAME, so the key is always the user's. The security runtime
    (OpenCode) already speaks most of these providers, so the ranked set widens with a
    verification run, not adapter code — which is how DeepSeek V4 Flash joined it at
    P38g."""
    load_seed(db_session)
    headers = _auth_header(client)

    body = client.post(
        "/recommendations",
        json={"taskTypes": ["security_analysis"], "budgetSensitivity": "high"},
        headers=headers,
    ).json()

    group = body["comparabilityGroup"]
    assert group["candidateCount"] == 16  # everything RealVuln scored
    # what the agent can drive: DeepSeek V4 Flash, Gemini 3.5 Flash, Opus 5
    assert group["rankedCount"] == 3
    assert group["rankedCount"] < group["candidateCount"]

    # the cost-leaning security pick comes from the runnable set. Since P38g that is
    # also the cheapest scored row outright — before it, the cheapest was excluded and
    # the pick was the cheapest RUNNABLE one, which is the case this filter exists for.
    assert body["suggested"]["model"] == "DeepSeek V4 Flash"
    assert body["baseline"]["model"] == "Claude Opus 5"

    # and the full 16 rows are still in the catalog — the narrowing is the PICK's,
    # not the data's
    listed = client.get("/benchmarks", headers=headers).json()
    security_rows = [r for r in listed if r["taskType"] == "security_analysis"]
    assert len(security_rows) == 16


def test_unrunnable_models_can_be_ranked_when_the_filter_is_off(
    client, db_session, monkeypatch
):
    """The restriction is a policy, not a hard-wired rule: RECOMMEND_ONLY_RUNNABLE=false
    ranks the whole catalog. Kept switchable so the honest-breadth view is one env var
    away, and so this test documents exactly what the filter changes."""
    from app.config import get_settings

    load_seed(db_session)
    headers = _auth_header(client)

    monkeypatch.setenv("RECOMMEND_ONLY_RUNNABLE", "false")
    get_settings.cache_clear()
    try:
        body = client.post(
            "/recommendations",
            json={"taskTypes": ["security_analysis"], "budgetSensitivity": "high"},
            headers=headers,
        ).json()
    finally:
        get_settings.cache_clear()

    # unfiltered, the cheapest strong scanner wins on the formula — a model we have no
    # way to run, which is exactly why the filter exists
    assert body["comparabilityGroup"]["rankedCount"] == 16
    assert body["suggested"]["model"] == "DeepSeek V4 Flash"
