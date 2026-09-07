"""P38c: the hard one-(benchmark, metric)-per-task-type invariant.

The recommender only ever compares rows that share a benchmark AND a metric. Before
P38c that rule was enforced by a VOTE at rank time — the group with the most rows won
and the rest were silently dropped, so a task type could accumulate rows from two
benchmarks and the pick would quietly depend on which side had more rows.

P38c makes it a hard invariant, enforced at BOTH ends:

  * at write time (here) — a row whose task type already belongs to a different
    (benchmark, metric) pair is REJECTED, so the bad state cannot be created; and
  * at rank time (tests/test_recommend_scoring.py) — if two groups ever survive the
    filter anyway, scoring RAISES instead of voting.

Together: one task type means exactly one comparability group, always.
"""

import pytest
from sqlalchemy import func, select

from app.catalog import service
from app.catalog.seed import load_seed
from app.catalog.service import TaskBenchmarkConflict
from app.models import BenchmarkResult
from app.schemas.catalog import CatalogRowIn


def _row(**over) -> CatalogRowIn:
    base = dict(
        model="Claude Haiku 4.5",
        vendor="Anthropic",
        benchmark="CodeReviewBench",
        metric="review_score_percent",
        score=85.0,
        cost_per_mtok=2.0,
        task_type="ci_review",
    )
    base.update(over)
    return CatalogRowIn(**base)


def test_second_benchmark_for_the_same_task_type_is_rejected(db_session):
    """The core invariant: ci_review belongs to ONE benchmark."""
    service.upsert_catalog_row(db_session, _row())

    with pytest.raises(TaskBenchmarkConflict) as exc:
        service.upsert_catalog_row(
            db_session, _row(model="Gemini 3.5 Flash", vendor="Google", benchmark="RealVuln")
        )

    # The error names both sides + the task type, so an operator can act on it.
    msg = str(exc.value)
    assert "ci_review" in msg
    assert "CodeReviewBench" in msg
    assert "RealVuln" in msg

    # and nothing was written — the reject happens BEFORE any row is created.
    assert db_session.scalar(select(func.count()).select_from(BenchmarkResult)) == 1


def test_second_metric_for_the_same_task_type_is_rejected(db_session):
    """Same benchmark but a different metric is still a second comparability group
    (an F1 row must never be normalized against a recall row), so it is rejected too."""
    service.upsert_catalog_row(db_session, _row())

    with pytest.raises(TaskBenchmarkConflict) as exc:
        service.upsert_catalog_row(db_session, _row(metric="f1_score"))

    assert "review_score_percent" in str(exc.value)
    assert "f1_score" in str(exc.value)


def test_a_different_task_type_may_use_a_different_benchmark(db_session):
    """The invariant is per task type — security_analysis is free to use RealVuln."""
    service.upsert_catalog_row(db_session, _row())
    service.upsert_catalog_row(
        db_session,
        _row(
            model="Gemini 3.5 Flash",
            vendor="Google",
            benchmark="RealVuln",
            metric="f3_score",
            score=35.5,
            cost_per_mtok=4.125,
            task_type="security_analysis",
        ),
    )
    assert db_session.scalar(select(func.count()).select_from(BenchmarkResult)) == 2


def test_re_upserting_the_same_group_still_works(db_session):
    """The invariant must not break idempotency: the seed re-upserts every row on
    every sync, and other models join the same (benchmark, metric) group freely."""
    service.upsert_catalog_row(db_session, _row())
    service.upsert_catalog_row(db_session, _row(score=86.0))  # same identity, refreshed
    service.upsert_catalog_row(
        db_session, _row(model="Claude Sonnet 4.5", score=87.1, cost_per_mtok=6.0)
    )

    rows = service.list_catalog(db_session)
    assert len(rows) == 2
    assert float(next(r for r in rows if r.model == "Claude Haiku 4.5").score) == 86.0


def test_rows_without_a_task_type_are_not_constrained(db_session):
    """task_type is nullable; an untyped row belongs to no task, so it cannot
    conflict with one (it is also never rankable — recommend filters on task_type)."""
    service.upsert_catalog_row(db_session, _row(task_type=None))
    service.upsert_catalog_row(
        db_session, _row(benchmark="RealVuln", metric="f3_score", task_type=None)
    )
    assert db_session.scalar(select(func.count()).select_from(BenchmarkResult)) == 2


def test_the_shipped_seed_satisfies_the_invariant(db_session):
    """Regression guard on the seed file itself: loading it must never trip the
    invariant, and every task type must map to exactly one (benchmark, metric)."""
    load_seed(db_session)

    groups: dict[str, set[tuple[str, str]]] = {}
    for row in service.list_catalog(db_session):
        if row.task_type:
            groups.setdefault(row.task_type, set()).add((row.benchmark, row.metric))

    assert groups, "seed produced no task-typed rows"
    for task_type, pairs in groups.items():
        assert len(pairs) == 1, f"{task_type} spans multiple groups: {sorted(pairs)}"
