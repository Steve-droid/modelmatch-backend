"""S7 keyword pre-fill tests: deterministic, no LLM, no model call.

Known terms map to form fields, unknown text is ignored, conflicts resolve to None,
and word boundaries keep "top" from firing inside "laptop".
"""

from app.recommend.prefill import prefill


def test_maps_known_terms_to_form_fields():
    res = prefill("I need a cheap coding agent for my repo")
    assert res.task_types == ["agentic_coding"]
    assert res.budget_sensitivity == "high"
    assert "cheap" in res.matched_terms


def test_quality_leaning_and_long_context():
    res = prefill("best model for long context RAG work")
    assert res.task_types == ["long_context"]
    assert res.budget_sensitivity == "low"  # "best"


def test_latency_terms_map():
    res = prefill("a fast, interactive assistant")
    assert res.latency_need == "low"


def test_conflicting_budget_terms_resolve_to_none():
    # both a high-sensitivity ("cheap") and low-sensitivity ("best") term fire
    res = prefill("a cheap but best option")
    assert res.budget_sensitivity is None  # conflict → don't guess
    assert "cheap" in res.matched_terms and "best" in res.matched_terms


def test_word_boundary_avoids_false_match():
    res = prefill("I run this on my laptop")
    assert res.budget_sensitivity is None  # "top" must NOT match inside "laptop"


def test_unknown_text_yields_empty_suggestion():
    res = prefill("hello world foobar")
    assert res.task_types == []
    assert res.budget_sensitivity is None
    assert res.latency_need is None
    assert res.matched_terms == []


def test_deterministic():
    text = "cheap autonomous coding agent, low latency"
    assert prefill(text) == prefill(text)


def test_prefill_endpoint_requires_auth(client):
    assert client.post("/recommendations/prefill", json={"text": "cheap agent"}).status_code == 401


def test_prefill_endpoint_returns_suggestion(client):
    creds = {"email": "pf@example.com", "password": "correct horse battery"}
    client.post("/auth/register", json=creds)
    token = client.post("/auth/login", json=creds).json()["accessToken"]
    resp = client.post(
        "/recommendations/prefill",
        json={"text": "cheap coding agent"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["taskTypes"] == ["agentic_coding"]
    assert body["budgetSensitivity"] == "high"
