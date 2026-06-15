"""Corpus-driven ingestion tests (P18) — all OFFLINE on the fake client (zero tokens).

Drives the tracked free-text fixture corpus under tests/fixtures/ingestion_corpus/
(model cards, leaderboards, pricing pages, technical reports, release posts, negatives),
centered on CodeReviewBench (the product's CI-review proof). Each case pairs a free-text
source with the canned LLM extraction output it should yield; `manifest.json` records the
expected offline outcome (status / rowsCreated / rowsRejected).

Two layers:
  * PURE parse/validate over the WHOLE corpus (no DB → fast lane): proves the untrusted
    output validator yields exactly the expected valid/rejected counts and never invents
    a missing field.
  * DB ingest over a representative subset + the idempotency, cross-source dedupe, and
    split-price-derive behaviours (auto-marked `integration` → full lane).

Real Bedrock is NEVER called here — that is the gated e2e-live path only.
"""

import json
from pathlib import Path

import pytest

from app.ingest.service import ingest_source
from app.ingest.validation import parse_catalog_rows
from app.llm.fake import FakeLLMClient
from app.schemas.ingest import IngestRequest

CORPUS = Path(__file__).resolve().parent / "fixtures" / "ingestion_corpus"
MANIFEST = json.loads((CORPUS / "manifest.json").read_text())
BY_ID = {c["id"]: c for c in MANIFEST}
CATEGORIES = {"leaderboards", "model_cards", "pricing_pages", "technical_reports",
              "release_posts", "negative_cases"}


def _source_text(case) -> str:
    return (CORPUS / case["source"]).read_text()


def _response_text(case) -> str:
    return (CORPUS / case["response"]).read_text()


def _req(case) -> IngestRequest:
    return IngestRequest(source_text=_source_text(case), kind=case["kind"])


# --- corpus integrity -----------------------------------------------------------------


def test_corpus_is_large_and_covers_every_category():
    assert len(MANIFEST) >= 25, "corpus should hold 25-40 cases"
    present = {c["category"] for c in MANIFEST}
    assert present == CATEGORIES, f"missing categories: {CATEGORIES - present}"
    # every declared source + response file actually exists
    for c in MANIFEST:
        assert (CORPUS / c["source"]).is_file(), f"missing source {c['source']}"
        assert (CORPUS / c["response"]).is_file(), f"missing response {c['response']}"
    # every source is labelled as fixture/demo text (not a real cited benchmark)
    for c in MANIFEST:
        assert "FIXTURE" in _source_text(c), f"{c['source']} missing the FIXTURE label"


def test_codereviewbench_is_the_primary_group():
    """The product proof is CI code review, so CodeReviewBench must dominate the corpus
    and SWE-bench Verified is only a secondary breadth group."""
    crb = sum(1 for c in MANIFEST if c["group"] == "codereviewbench")
    swe = sum(1 for c in MANIFEST if c["group"] == "swebench")
    assert crb > swe, "CodeReviewBench must be the primary (dominant) ingestion group"
    assert swe >= 1, "keep at least one SWE-bench (agentic_coding) breadth case"


# --- PURE parse/validate over the WHOLE corpus (fast lane, no DB) ----------------------


@pytest.mark.parametrize("case", MANIFEST, ids=[c["id"] for c in MANIFEST])
def test_corpus_response_parses_to_expected_counts(case):
    valid, rejections = parse_catalog_rows(_response_text(case))
    assert len(valid) == case["rowsCreated"], f"{case['id']}: valid-row count"
    assert len(rejections) == case["rowsRejected"], f"{case['id']}: rejection count"
    # every surviving row carries the required fields (the validator never invents them)
    for row in valid:
        assert row.model and row.vendor and row.benchmark and row.metric
        assert row.score is not None and row.cost_per_mtok is not None


def test_corpus_primary_rows_are_codereviewbench_ci_review():
    """Across all extracted valid rows, CodeReviewBench rows outnumber SWE-bench rows and
    are tagged review_score_percent / ci_review (the recommender's primary path)."""
    crb = swe = 0
    for c in MANIFEST:
        valid, _ = parse_catalog_rows(_response_text(c))
        for row in valid:
            if row.benchmark == "CodeReviewBench":
                crb += 1
                assert row.metric == "review_score_percent"
                assert row.task_type == "ci_review"
            elif row.benchmark == "SWE-bench Verified":
                swe += 1
                assert row.metric == "pass@1_percent"
    assert crb > swe, f"CodeReviewBench rows ({crb}) must dominate SWE-bench ({swe})"


# --- DB ingest over a representative subset (full lane) --------------------------------

# One per category + every outcome type (multi-row, partial, pricing-only/no-rows,
# secondary SWE-bench, harness fields, envelope reject, out-of-bounds, missing fields).
_REPRESENTATIVE = [
    "leaderboards/codereviewbench-top",
    "leaderboards/codereviewbench-partial-one-bad",
    "model_cards/claude-sonnet",
    "pricing_pages/anthropic-pricing",
    "technical_reports/nova-2-report",
    "technical_reports/swe-agent-harness",
    "release_posts/haiku-launch",
    "negative_cases/model-refusal-prose",
    "negative_cases/out-of-bounds",
    "negative_cases/missing-required-fields",
    "negative_cases/mixed-valid-invalid",
]


@pytest.mark.parametrize("case_id", _REPRESENTATIVE)
def test_corpus_full_ingest_matches_manifest(db_session, case_id):
    case = BY_ID[case_id]
    fake = FakeLLMClient(responses=_response_text(case))
    result = ingest_source(db_session, _req(case), fake)
    assert result.status == case["status"], f"{case_id}: status"
    assert result.rows_created == case["rowsCreated"], f"{case_id}: rowsCreated"
    assert result.rows_rejected == case["rowsRejected"], f"{case_id}: rowsRejected"


def test_corpus_idempotent_repost_skips(db_session):
    """A reposted source (byte-identical after normalisation) re-ingests as `skipped` —
    no second LLM call, no duplicate rows."""
    original = BY_ID["model_cards/claude-haiku"]
    repost = BY_ID["model_cards/haiku-reposted"]
    assert repost["dedupesWith"] == original["id"]

    fake = FakeLLMClient(responses=_response_text(original))
    first = ingest_source(db_session, _req(original), fake)
    assert first.status == "ingested"
    assert fake._i == 1

    second = ingest_source(db_session, _req(repost), fake)
    assert second.status == "skipped"
    assert second.source_document_id == first.source_document_id
    assert fake._i == 1  # not called again → zero tokens


def test_corpus_cross_source_identity_dedupes_to_one_row(db_session):
    """Nova Lite's CodeReviewBench result appears in BOTH the model card and the
    leaderboard. Two distinct sources, same (model, benchmark, metric) identity → ONE
    catalog row (upsert), not two."""
    from sqlalchemy import func, select

    from app.models import BenchmarkResult, Model, SourceDocument

    card = BY_ID["model_cards/nova-lite"]
    board = BY_ID["leaderboards/codereviewbench-top"]

    ingest_source(db_session, _req(card), FakeLLMClient(responses=_response_text(card)))
    ingest_source(db_session, _req(board), FakeLLMClient(responses=_response_text(board)))

    # two source documents recorded
    assert db_session.scalar(select(func.count()).select_from(SourceDocument)) == 2
    # but Nova Lite's CodeReviewBench row exists exactly once
    nova = db_session.scalar(select(Model).where(Model.name == "Amazon Nova Lite"))
    assert nova is not None
    nova_crb = db_session.scalar(
        select(func.count()).select_from(BenchmarkResult).where(
            BenchmarkResult.model_id == nova.id
        )
    )
    assert nova_crb == 1


def test_corpus_split_price_derives_ranking_cost(db_session):
    """A pricing page that also cites the headline score stores the SPLIT prices as-is
    and DERIVES the ranking cost (3:1 blend), not the LLM's costPerMtok placeholder:
    Haiku 0.80 in / 4.00 out → (3*0.80 + 4.00)/4 = 1.60."""
    from decimal import Decimal

    from sqlalchemy import select

    from app.models import Model

    case = BY_ID["pricing_pages/haiku-pricing-with-score"]
    result = ingest_source(db_session, _req(case), FakeLLMClient(responses=_response_text(case)))
    assert result.status == "ingested" and result.rows_created == 1

    haiku = db_session.scalar(select(Model).where(Model.name == "Claude Haiku 4.5"))
    assert haiku.input_price_per_mtok == Decimal("0.80")
    assert haiku.output_price_per_mtok == Decimal("4.00")
    assert haiku.price_per_mtok == Decimal("1.60")  # derived, not the 0.80 placeholder
