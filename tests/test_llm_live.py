"""Gated live LLM smoke — deliberately hits real models, so it's OFF by default.

Run with real keys to validate the adapters end-to-end:
    RUN_LLM_LIVE=1 ANTHROPIC_API_KEY=... uv run pytest tests/test_llm_live.py
Each test also skips if its SDK or key is missing. Costs a few tokens — mock-first
everywhere else; this is the only path that spends.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LLM_LIVE") != "1",
    reason="set RUN_LLM_LIVE=1 to hit real models (spends tokens)",
)


def test_anthropic_live():
    pytest.importorskip("anthropic")
    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    from app.llm import build_llm_client

    client = build_llm_client(
        "anthropic", model=os.getenv("LIVE_ANTHROPIC_MODEL", "claude-3-5-haiku-latest")
    )
    resp = client.complete("You are terse.", "Reply with the single word: ok", 16)
    assert resp.text.strip()
    assert resp.tokens_out > 0


def test_gemini_live():
    pytest.importorskip("google.genai")
    if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        pytest.skip("GEMINI_API_KEY / GOOGLE_API_KEY not set")
    from app.llm import build_llm_client

    client = build_llm_client(
        "gemini", model=os.getenv("LIVE_GEMINI_MODEL", "gemini-2.0-flash")
    )
    resp = client.complete("You are terse.", "Reply with the single word: ok", 16)
    assert resp.text.strip()


def test_bedrock_nova_ingest_live():
    """Real in-cluster ingestion smoke: Nova extracts catalog rows from a tiny source,
    and our untrusted-output validator parses at least one valid row. No DB — this
    exercises the adapter + prompt + validation end-to-end. Needs AWS creds (IRSA
    locally = a profile/role allowing bedrock:Converse in the region)."""
    pytest.importorskip("boto3")
    from app.ingest.prompts import INGEST_SYSTEM_PROMPT, build_user_prompt
    from app.ingest.validation import parse_catalog_rows
    from app.llm import build_llm_client

    # ap-south-1 needs the apac. inference profile (bare id rejected on-demand).
    model = os.getenv("LIVE_BEDROCK_MODEL", "apac.amazon.nova-lite-v1:0")
    region = os.getenv("AWS_REGION", "ap-south-1")
    client = build_llm_client("bedrock", model=model, region=region)

    source = (
        "Amazon Nova Lite scores 38% pass@1 on SWE-bench Verified, about $0.06 per "
        "million tokens, 300k context, measured 2026-01-20."
    )
    try:
        resp = client.complete(INGEST_SYSTEM_PROMPT, build_user_prompt(source), 512)
    except Exception as exc:  # no creds / model not enabled in this account
        pytest.skip(f"Bedrock Nova not reachable: {exc}")

    valid, _ = parse_catalog_rows(resp.text)
    assert valid, f"Nova returned no valid catalog rows: {resp.text!r}"
    assert valid[0].model and valid[0].score is not None


def test_bedrock_nova_ingest_end_to_end(db_session):
    """Real end-to-end ingest against Nova + Postgres: source → extract → validate →
    upsert, then a re-ingest of the same bytes SKIPS (idempotency, zero extra calls).
    Proves the full orchestrator path the offline tests fake. (db_session skips with no
    DB; the try/except skips with no Bedrock — so this only runs when both are live.)"""
    pytest.importorskip("boto3")
    from sqlalchemy import func, select

    from app.ingest.service import ingest_source
    from app.llm import build_llm_client
    from app.models import BenchmarkResult
    from app.schemas.ingest import IngestRequest

    model = os.getenv("LIVE_BEDROCK_MODEL", "apac.amazon.nova-lite-v1:0")
    region = os.getenv("AWS_REGION", "ap-south-1")
    client = build_llm_client("bedrock", model=model, region=region)

    req = IngestRequest(
        source_text=(
            "Amazon Nova Lite scores 38% pass@1 on SWE-bench Verified, about $0.06 "
            "per million tokens, 300k context, measured 2026-01-20."
        ),
        kind="model_card",
    )
    try:
        result = ingest_source(db_session, req, client)
    except Exception as exc:  # no creds / model not enabled
        pytest.skip(f"Bedrock Nova not reachable: {exc}")

    assert result.status == "ingested"
    assert result.rows_created >= 1
    rows = db_session.scalars(select(BenchmarkResult)).all()
    assert rows and all(r.source_document_id == result.source_document_id for r in rows)

    # re-ingesting identical bytes short-circuits — no second Nova call (zero tokens).
    again = ingest_source(db_session, req, client)
    assert again.status == "skipped"
    assert again.source_document_id == result.source_document_id
    assert db_session.scalar(select(func.count()).select_from(BenchmarkResult)) == len(rows)
