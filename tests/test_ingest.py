"""S5b LLM-ingestion tests — all offline on the fake client (zero tokens, zero cost).

Covers the slice's contracts: content-hash idempotency (unchanged source → no
re-work, no LLM call), untrusted-output validation (bad JSON / out-of-bounds / partial
rows), idempotent upsert (no duplicate catalog rows), the hard hourly token cap
(aborts before spending), and the route (auth, happy path, 429).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.blob_store import InMemoryBlobStore, source_key
from app.config import get_settings
from app.ingest.service import _normalize, build_ingest_client, ingest_source
from app.llm.fake import FakeLLMClient
from app.llm_budget import HourlyTokenCapExceeded, _current_hour
from app.models import BenchmarkResult, LlmCall, LlmUsage, SourceDocument
from app.schemas.ingest import IngestRequest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_SOURCE = (FIXTURES / "sample_source.md").read_text()
NOVA_ROWS = (FIXTURES / "nova_response.json.txt").read_text()  # fenced, 2 valid rows


def _count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def _req(text=SAMPLE_SOURCE, **kw) -> IngestRequest:
    return IngestRequest(source_text=text, **kw)


# --- extraction → validation → upsert -------------------------------------------------


def test_ingest_extracts_validates_and_upserts(db_session):
    blob = InMemoryBlobStore()
    fake = FakeLLMClient(responses=NOVA_ROWS)

    result = ingest_source(db_session, _req(kind="model_card"), fake, blob=blob)

    assert result.status == "ingested"
    assert result.rows_created == 2
    assert result.rows_rejected == 0
    assert result.tokens_out > 0  # the fake derives tokens from text length

    # rows persisted + linked to the source document
    rows = db_session.scalars(select(BenchmarkResult)).all()
    assert len(rows) == 2
    assert {r.source_document_id for r in rows} == {result.source_document_id}

    # source document recorded with status + blob key; bytes actually stored
    doc = db_session.get(SourceDocument, result.source_document_id)
    assert doc.status == "ingested"
    assert doc.kind == "model_card"
    assert doc.s3_key == source_key(doc.content_hash)
    assert blob.get(doc.s3_key) == _normalize(SAMPLE_SOURCE)

    # exactly one ingestion llm_call row recorded
    calls = db_session.scalars(select(LlmCall)).all()
    assert len(calls) == 1
    assert calls[0].purpose == "ingestion"
    assert calls[0].ci_run_id is None
    assert calls[0].tokens_out > 0


def test_reingest_unchanged_source_skips_with_zero_tokens(db_session):
    fake = FakeLLMClient(responses=NOVA_ROWS)

    first = ingest_source(db_session, _req(), fake)
    assert first.status == "ingested"
    assert fake._i == 1  # called once
    rows_after_first = _count(db_session, BenchmarkResult)

    second = ingest_source(db_session, _req(), fake)  # identical bytes
    assert second.status == "skipped"
    assert second.source_document_id == first.source_document_id
    assert second.rows_created == 0
    assert fake._i == 1  # NOT called again → zero tokens

    assert _count(db_session, BenchmarkResult) == rows_after_first
    assert _count(db_session, LlmCall) == 1  # no new extraction


def test_whitespace_only_difference_still_dedupes(db_session):
    fake = FakeLLMClient(responses=NOVA_ROWS)
    ingest_source(db_session, _req(SAMPLE_SOURCE), fake)
    # CRLF + surrounding blank lines normalize to the same bytes → skipped.
    noisy = "\n\n" + SAMPLE_SOURCE.replace("\n", "\r\n") + "\n  "
    result = ingest_source(db_session, _req(noisy), fake)
    assert result.status == "skipped"
    assert fake._i == 1


# --- untrusted output: bad JSON / bounds / partial rows -------------------------------


def test_non_json_output_is_rejected(db_session):
    fake = FakeLLMClient(responses="I could not find any benchmarks, sorry!")
    result = ingest_source(db_session, _req(), fake)

    assert result.status == "invalid"
    assert result.rows_created == 0
    assert result.rows_rejected == 1
    assert result.rejections[0].index == -1  # whole-output failure
    assert _count(db_session, BenchmarkResult) == 0
    # the source is still recorded as invalid (the call happened, tokens were spent)
    doc = db_session.get(SourceDocument, result.source_document_id)
    assert doc.status == "invalid"
    assert _count(db_session, LlmCall) == 1


def test_partial_rows_keep_valid_drop_invalid(db_session):
    one_good_one_bad = json.dumps(
        {
            "rows": [
                {
                    "model": "Amazon Nova Lite", "vendor": "Amazon",
                    "benchmark": "MMLU", "metric": "accuracy_percent",
                    "score": 70.0, "costPerMtok": 0.06,
                },
                {  # missing required `vendor` → dropped, not stored
                    "model": "Ghost Model", "benchmark": "MMLU",
                    "metric": "accuracy_percent", "score": 50.0, "costPerMtok": 0.1,
                },
            ]
        }
    )
    fake = FakeLLMClient(responses=one_good_one_bad)
    result = ingest_source(db_session, _req(), fake)

    assert result.status == "ingested"
    assert result.rows_created == 1
    assert result.rows_rejected == 1
    assert result.rejections[0].index == 1
    assert _count(db_session, BenchmarkResult) == 1


def test_ingest_v2_captures_split_prices_and_derives_ranking_cost(db_session):
    """ingest-v2: SEPARATE input/output prices are stored as-is, and the ranking
    cost_per_mtok is DERIVED by the backend (3:1 blend), NOT taken from the LLM's
    costPerMtok placeholder. This is what keeps seeded and ingested rows on one cost
    basis. Source gives costPerMtok=1.0 but split 1.0/8.0 → ranking cost = 2.75."""
    from decimal import Decimal

    from app.ingest.prompts import PROMPT_VERSION
    from app.models import Model

    assert PROMPT_VERSION == "ingest-v2"  # the bump is in effect

    with_split = json.dumps(
        {
            "rows": [
                {
                    "model": "Split Priced Model", "vendor": "ACME",
                    "benchmark": "CodeReviewBench", "metric": "review_score_percent",
                    "score": 80.0, "costPerMtok": 1.0,
                    "inputPricePerMtok": 1.0, "outputPricePerMtok": 8.0,
                },
            ]
        }
    )
    fake = FakeLLMClient(responses=with_split)
    result = ingest_source(db_session, _req(), fake)

    assert result.status == "ingested"
    assert result.rows_created == 1
    m = db_session.scalar(select(Model).where(Model.name == "Split Priced Model"))
    # split prices stored from the source, NOT backfilled to input == output
    assert m.input_price_per_mtok == Decimal("1.0")
    assert m.output_price_per_mtok == Decimal("8.0")
    # ranking cost is the derived 3:1 blend (3*1 + 8)/4 = 2.75 — NOT the LLM's 1.0
    assert m.price_per_mtok == Decimal("2.75")
    br = db_session.scalar(
        select(BenchmarkResult).where(BenchmarkResult.model_id == m.id)
    )
    assert br.cost_per_mtok == Decimal("2.75")


def test_out_of_bounds_values_are_rejected(db_session):
    out_of_bounds = json.dumps(
        {
            "rows": [
                {  # score far beyond the Numeric(8,4) column ceiling
                    "model": "Overflow", "vendor": "X", "benchmark": "B",
                    "metric": "m", "score": 9999999, "costPerMtok": 1.0,
                },
                {  # negative cost
                    "model": "Negative", "vendor": "X", "benchmark": "B",
                    "metric": "m", "score": 1.0, "costPerMtok": -5,
                },
            ]
        }
    )
    fake = FakeLLMClient(responses=out_of_bounds)
    result = ingest_source(db_session, _req(), fake)

    assert result.status == "invalid"
    assert result.rows_created == 0
    assert result.rows_rejected == 2
    assert _count(db_session, BenchmarkResult) == 0


def test_rows_must_be_an_array(db_session):
    fake = FakeLLMClient(responses=json.dumps({"rows": {"not": "a list"}}))
    result = ingest_source(db_session, _req(), fake)
    assert result.status == "invalid"
    assert result.rejections[0].index == -1


# --- idempotent upsert: no duplicate catalog rows -------------------------------------


def test_two_sources_same_identity_dedupe_to_one_row(db_session):
    fake = FakeLLMClient(responses=NOVA_ROWS)

    first = ingest_source(db_session, _req("Source A: " + SAMPLE_SOURCE), fake)
    second = ingest_source(db_session, _req("Source B: " + SAMPLE_SOURCE), fake)

    # different sources (2 docs) but the same 2 row identities → 2 rows, not 4
    assert first.source_document_id != second.source_document_id
    assert _count(db_session, SourceDocument) == 2
    assert _count(db_session, BenchmarkResult) == 2

    # latest ingest wins provenance
    rows = db_session.scalars(select(BenchmarkResult)).all()
    assert {r.source_document_id for r in rows} == {second.source_document_id}


# --- the hard hourly token cap (must ABORT, not alert) --------------------------------


def test_hourly_cap_aborts_before_any_spend(db_session):
    fake = FakeLLMClient(responses=NOVA_ROWS)

    with pytest.raises(HourlyTokenCapExceeded):
        ingest_source(db_session, _req(), fake, hourly_cap=1)

    assert fake._i == 0  # provider never called
    assert _count(db_session, SourceDocument) == 0
    assert _count(db_session, BenchmarkResult) == 0
    assert _count(db_session, LlmCall) == 0

    # reservation released → the hour's tally is back to zero
    usage = db_session.get(LlmUsage, _current_hour())
    assert usage is None or usage.tokens_used == 0


def test_successful_ingest_records_actual_usage(db_session):
    fake = FakeLLMClient(responses=NOVA_ROWS)
    result = ingest_source(db_session, _req(), fake)

    db_session.expire_all()
    usage = db_session.get(LlmUsage, _current_hour())
    # reconciled to actual (estimate budgeted full output; actual is smaller)
    assert usage is not None
    assert usage.tokens_used == result.tokens_in + result.tokens_out


# --- the route ------------------------------------------------------------------------


def _auth_header(client) -> dict[str, str]:
    creds = {"email": "ingest@example.com", "password": "correct horse battery"}
    user_id = client.post("/auth/register", json=creds).json()["id"]
    # Set the role in the fixture DB, never through the public registration payload.
    from app.auth.deps import get_db
    from app.main import app
    from app.models import User
    session = app.dependency_overrides[get_db]()
    db = next(session)
    db.get(User, user_id).is_operator = True
    db.commit()
    session.close()
    token = client.post("/auth/login", json=creds).json()["accessToken"]
    return {"Authorization": f"Bearer {token}"}


def _override_client(responses):
    from app.main import app

    app.dependency_overrides[build_ingest_client] = lambda: FakeLLMClient(responses=responses)


def test_ingest_route_requires_auth(client):
    assert client.post("/benchmarks/ingest", json={"sourceText": "x"}).status_code == 401


def test_ingest_route_happy_path(client):
    _override_client(NOVA_ROWS)
    headers = _auth_header(client)

    resp = client.post("/benchmarks/ingest", json={"sourceText": SAMPLE_SOURCE}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ingested"
    assert body["rowsCreated"] == 2
    assert body["rowsRejected"] == 0

    listed = client.get("/benchmarks", headers=headers).json()
    assert len(listed) == 2


def test_ingest_route_cap_returns_429(client, monkeypatch):
    _override_client(NOVA_ROWS)
    headers = _auth_header(client)
    monkeypatch.setattr(get_settings(), "llm_hourly_token_cap", 1)

    resp = client.post("/benchmarks/ingest", json={"sourceText": SAMPLE_SOURCE}, headers=headers)
    assert resp.status_code == 429
    assert client.get("/benchmarks", headers=headers).json() == []  # nothing persisted
