"""S5 catalog tests: add/list via the API, and idempotent seed/upsert.

Through the TestClient (auth-gated) for the routes; through the service + ORM for
the idempotency guarantees (the natural-key upsert, incl. NULL-harness dedupe).
"""

from sqlalchemy import func, select

from app.catalog import service
from app.catalog.seed import load_seed
from app.models import AgentRuntimeConfig, Benchmark, BenchmarkResult, Model
from app.schemas.catalog import CatalogRowIn

ROW = {
    "model": "Claude Haiku 4.5",
    "vendor": "Anthropic",
    "benchmark": "SWE-bench Verified",
    "metric": "pass@1_percent",
    "score": 45.0,
    "costPerMtok": 0.8,
    "harness": "SWE-agent",
    "harnessVendor": "Princeton",
    "taskType": "agentic_coding",
    "contextWindow": 200000,
    "source": "vendor model card",
    "measuredAt": "2026-02-01",
}


def _auth_header(client) -> dict[str, str]:
    creds = {"email": "cat@example.com", "password": "correct horse battery"}
    client.post("/auth/register", json=creds)
    token = client.post("/auth/login", json=creds).json()["accessToken"]
    return {"Authorization": f"Bearer {token}"}


def test_post_creates_normalized_rows_and_get_lists_them(client, db_session):
    headers = _auth_header(client)

    resp = client.post("/benchmarks", json=ROW, headers=headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["model"] == "Claude Haiku 4.5"
    assert float(body["costPerMtok"]) == 0.8
    assert "id" in body

    listed = client.get("/benchmarks", headers=headers)
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["benchmark"] == "SWE-bench Verified"
    assert rows[0]["harness"] == "SWE-agent"
    assert rows[0]["harnessVendor"] == "Princeton"

    # dimensions were get-or-created: the migration may already have inserted trusted
    # runtime-capable model rows, but posting this catalog row must still dedupe the
    # Claude model and create exactly one benchmark dimension.
    assert db_session.scalar(
        select(func.count()).select_from(Model).where(
            Model.name == "Claude Haiku 4.5", Model.vendor == "Anthropic"
        )
    ) == 1
    assert db_session.scalar(select(func.count()).select_from(Benchmark)) == 1


def test_get_and_post_require_auth(client):
    assert client.get("/benchmarks").status_code == 401
    assert client.post("/benchmarks", json=ROW).status_code == 401


def test_upsert_refreshes_figures_without_duplicating(client, db_session):
    headers = _auth_header(client)
    client.post("/benchmarks", json=ROW, headers=headers)
    client.post("/benchmarks", json={**ROW, "score": 47.5, "costPerMtok": 0.9}, headers=headers)

    rows = client.get("/benchmarks", headers=headers).json()
    assert len(rows) == 1  # same identity → updated, not duplicated
    assert float(rows[0]["score"]) == 47.5
    assert float(rows[0]["costPerMtok"]) == 0.9


def test_seed_loads_idempotently(db_session):
    first = load_seed(db_session)
    count_after_first = db_session.scalar(select(func.count()).select_from(BenchmarkResult))
    assert count_after_first == first  # one row per seed entry

    load_seed(db_session)  # re-load
    count_after_second = db_session.scalar(select(func.count()).select_from(BenchmarkResult))
    assert count_after_second == count_after_first  # no duplicates

    # models deduped across rows (Claude Haiku 4.5 appears twice: ci_review + agentic_coding)
    model_count = db_session.scalar(select(func.count()).select_from(Model))
    assert model_count == 7


def test_seed_loads_demo_agent_runtime_configs_idempotently(db_session):
    load_seed(db_session)
    load_seed(db_session)

    rows = db_session.scalars(
        select(AgentRuntimeConfig).join(Model).order_by(Model.vendor, Model.name)
    ).all()
    assert len(rows) == 3

    by_model = {row.model.name: row for row in rows}
    haiku = by_model["Claude Haiku 4.5"]
    assert haiku.provider == "anthropic"
    assert haiku.provider_model_id == "claude-haiku-4-5"
    assert haiku.auth_mode == "api_key"
    assert haiku.credential_env_var == "ANTHROPIC_API_KEY"
    assert haiku.enabled is True

    nova = by_model["Nova 2 Lite"]
    assert nova.provider == "bedrock"
    assert nova.provider_model_id == "global.amazon.nova-2-lite-v1:0"
    assert nova.auth_mode == "aws_iam"
    assert nova.credential_env_var is None
    assert nova.enabled is True

    gemini = by_model["Gemini 2.5 Flash"]
    assert gemini.provider == "gemini"
    assert gemini.provider_model_id == "gemini-2.5-flash"
    assert gemini.auth_mode == "api_key"
    assert gemini.credential_env_var == "GOOGLE_API_KEY"
    assert gemini.enabled is True


def test_null_harness_dedupes_via_nulls_not_distinct(db_session):
    base = CatalogRowIn(
        model="Amazon Nova Lite", vendor="Amazon", benchmark="MRCR (long-context)",
        metric="accuracy_percent", score=70.0, cost_per_mtok=0.06, harness=None,
        task_type="long_context",
    )
    service.upsert_catalog_row(db_session, base)
    service.upsert_catalog_row(db_session, base.model_copy(update={"score": 72.0}))

    rows = service.list_catalog(db_session)
    assert len(rows) == 1  # NULL harness still dedupes (NULLS NOT DISTINCT)
    assert float(rows[0].score) == 72.0
