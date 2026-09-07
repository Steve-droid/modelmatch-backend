"""P38c: the deterministic RealVuln ingest (NO LLM).

Guards the three properties that make this path trustworthy:

  * **purity** — the same upstream bytes always produce the same rows, so a catalog
    refresh is reviewable as a diff rather than a re-extraction;
  * **provenance** — every row cites the benchmark version, the date and where its
    price came from, and a model with no published price is skipped rather than
    given an invented one; and
  * **idempotency** — re-running over unchanged input changes nothing.

Runs against the checked-in copies under `data/catalog/`, so it needs no network.
"""

import json
from decimal import Decimal

from sqlalchemy import func, select

from app.catalog import ingest_realvuln as rv
from app.catalog.service import list_catalog
from app.models import BenchmarkResult, SourceDocument

DASHBOARD = rv.DEFAULT_DASHBOARD.read_bytes()
MODELS = rv.DEFAULT_MODELS.read_bytes()


def _summary():
    return rv.build_rows(DASHBOARD, MODELS)


def test_build_rows_is_pure() -> None:
    """Same bytes in, same rows out — the property that lets a refresh be reviewed
    as a diff instead of trusted as a black box."""
    a, b = _summary(), _summary()
    assert [(r.model, r.score, r.input_price_per_mtok) for r in a.rows] == [
        (r.model, r.score, r.input_price_per_mtok) for r in b.rows
    ]
    assert a.content_hash == b.content_hash


def test_scores_are_read_verbatim_from_the_dashboard() -> None:
    """No arithmetic on the score itself: F3 is copied, never recomputed or scaled."""
    aggregates = json.loads(DASHBOARD)["aggregates"]
    by_model = {r.model: r for r in _summary().rows}

    assert by_model["Gemini 3.5 Flash"].score == Decimal(
        str(aggregates["gemini-3.5-flash-agentic-v1"]["micro"]["f3_score"])
    )
    assert by_model["Claude Opus 5"].score == Decimal(
        str(aggregates["claude-opus-5-cc-agentic-v1"]["micro"]["f3_score"])
    )


def test_prices_come_from_the_benchmarks_own_price_table() -> None:
    """Prices are joined from RealVuln's models.yaml on its own scanner_slug — the
    figures are not typed in by hand, so they cannot drift from the source."""
    gemini = next(r for r in _summary().rows if r.model == "Gemini 3.5 Flash")
    assert gemini.input_price_per_mtok == Decimal("1.5")
    assert gemini.output_price_per_mtok == Decimal("9.0")
    assert "models.yaml" in gemini.source


def test_opus_5_is_priced_from_the_provider_and_says_so() -> None:
    """RealVuln does not price its Claude Code runs, so the baseline's price comes
    from Anthropic's public pricing page — and the row states that."""
    opus = next(r for r in _summary().rows if r.model == "Claude Opus 5")
    assert (opus.input_price_per_mtok, opus.output_price_per_mtok) == (
        Decimal("5.00"),
        Decimal("25.00"),
    )
    assert "platform.claude.com" in opus.source


def test_every_row_cites_a_dated_versioned_source() -> None:
    """The `source` string is the only provenance that reaches the API and the chat."""
    summary = _summary()
    for row in summary.rows:
        assert "RealVuln v2.1.0" in row.source
        assert str(summary.generated_at) in row.source
        assert row.measured_at == summary.generated_at
        assert row.benchmark_as_of == summary.generated_at


def test_harness_is_recorded_because_it_changes_the_score() -> None:
    """The same model under a different agent loop is a different measurement."""
    by_model = {r.model: r for r in _summary().rows}
    assert by_model["Claude Opus 5"].harness == "Claude Code"
    assert by_model["Gemini 3.5 Flash"].harness == "OpenCode"
    assert by_model["GPT-5.6 Sol"].harness == "Codex CLI"
    assert all("run under" in r.source for r in _summary().rows)


def test_vendor_products_and_static_analysers_are_skipped_with_reasons() -> None:
    """A recommender ranks MODELS. The benchmark author's own products and the SAST
    baselines are excluded deliberately, and the reason is reported, not silent."""
    skipped = _summary().skipped
    assert "semgrep" in skipped and "sonarqube" in skipped
    assert any(slug.startswith("kolega-") for slug in skipped)
    assert all(skipped.values()), "every skip must state a reason"

    models = {r.model for r in _summary().rows}
    assert not any(m.lower().startswith("kolega") for m in models)


def test_locally_hosted_zero_price_models_are_skipped() -> None:
    """A $0 row would win any cost-leaning ranking by construction, and 'savings vs
    baseline' is meaningless against a model running on your own hardware."""
    skipped = _summary().skipped
    for slug in ("qwen3.6-35b-agentic-v1", "gemma4-31b-agentic-v1", "ornith-q3-agentic-v1"):
        assert "zero" in skipped[slug]


def test_all_rows_form_exactly_one_comparability_group() -> None:
    """The whole import must satisfy the P38c invariant on its own."""
    rows = _summary().rows
    assert {(r.benchmark, r.metric, r.task_type) for r in rows} == {
        ("RealVuln", "f3_score", "security_analysis")
    }


def test_ingest_to_db_is_idempotent(db_session) -> None:
    """Re-running over unchanged input writes the same values and adds nothing —
    the property the scheduled refresh in a later slice depends on."""
    summary = _summary()

    first = rv.ingest_to_db(db_session, summary)
    count_after_first = db_session.scalar(select(func.count()).select_from(BenchmarkResult))
    assert first["rows"] == len(summary.rows)
    assert first["already_ingested"] is False
    assert count_after_first == len(summary.rows)

    second = rv.ingest_to_db(db_session, summary)
    assert second["already_ingested"] is True
    assert db_session.scalar(select(func.count()).select_from(BenchmarkResult)) == count_after_first
    # one source document for the one dashboard, not one per run
    assert db_session.scalar(select(func.count()).select_from(SourceDocument)) == 1


def test_stored_cost_is_the_derived_blend_not_the_raw_input_price(db_session) -> None:
    """The service owns the 3:1 blend; the ingest never asserts a ranking cost."""
    rv.ingest_to_db(db_session, _summary())
    gemini = next(r for r in list_catalog(db_session) if r.model == "Gemini 3.5 Flash")
    # (3 * 1.50 + 9.00) / 4
    assert gemini.cost_per_mtok == Decimal("3.375000")
    assert gemini.input_price_per_mtok == Decimal("1.500000")


def test_benchmark_provenance_reaches_the_catalog(db_session) -> None:
    """The dashboard's date lands on the benchmark row, so the UI and the chat can
    say how old the figures are."""
    summary = _summary()
    rv.ingest_to_db(db_session, summary)
    row = next(r for r in list_catalog(db_session) if r.benchmark == "RealVuln")
    assert row.benchmark_as_of == summary.generated_at
    assert "F3 weights recall" in row.benchmark_notes


def test_write_seed_rows_is_deterministic(tmp_path) -> None:
    """Regenerating the seed twice produces byte-identical output, so a refresh shows
    up as a clean diff (or no diff at all)."""
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"benchmark_results": [
        {"model": "Claude Haiku 4.5", "benchmark": "CodeReviewBench"},
    ]}))

    rows = _summary().rows
    rv.write_seed_rows(rows, seed)
    once = seed.read_text()
    rv.write_seed_rows(rows, seed)
    assert seed.read_text() == once

    data = json.loads(once)
    # the non-RealVuln row is preserved, the RealVuln rows are sorted by score desc
    assert data["benchmark_results"][0]["benchmark"] == "CodeReviewBench"
    realvuln = [e for e in data["benchmark_results"] if e["benchmark"] == "RealVuln"]
    assert [e["score"] for e in realvuln] == sorted(
        (e["score"] for e in realvuln), reverse=True
    )
