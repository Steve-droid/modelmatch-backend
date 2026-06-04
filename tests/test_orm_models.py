"""S3 ORM tests: models round-trip through the real schema, and never drift from it.

- Round-trip: insert a representative graph via the Session, read it back through
  relationships; enum + array columns survive; the recommendation_evidence join
  table enforces its FKs (the S2 review ask).
- Parity: the ORM's tables/columns match the migrated schema exactly — so
  `alembic revision --autogenerate` against these models yields no spurious diff.
"""

from datetime import date

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.models import Base
from app.models import (
    Benchmark,
    BenchmarkResult,
    Harness,
    Model,
    Project,
    RecommendationEvidence,
    RecommendationOption,
    RequirementsProfile,
    SourceDocument,
    User,
)


def test_models_round_trip_through_relationships(db_session):
    user = User(email="dev@example.com", password_hash="argon2-hash-placeholder")
    model = Model(name="Claude Haiku", vendor="Anthropic", data_policy="private")
    harness = Harness(name="SWE-agent", vendor="Princeton")
    benchmark = Benchmark(name="SWE-bench Verified", task_type="agentic_coding")
    source = SourceDocument(content_hash="abc123", kind="model_card", status="ingested")
    db_session.add_all([user, model, harness, benchmark, source])
    db_session.flush()

    result = BenchmarkResult(
        model_id=model.id,
        harness_id=harness.id,
        benchmark_id=benchmark.id,
        task_type="agentic_coding",
        score=42.2,
        metric="pass@1_percent",
        cost_per_mtok="0.27",
        context_window=65536,
        source="https://example.test",
        source_document_id=source.id,
        measured_at=date(2025, 7, 2),
    )
    profile = RequirementsProfile(
        user_id=user.id,
        task_types=["agentic_coding", "long_context"],  # array column
        budget_sensitivity="high",  # native enum
        latency_need="low",
    )
    db_session.add_all([result, profile])
    db_session.flush()

    option = RecommendationOption(profile_id=profile.id, rank=1, rank_score="0.83", model_id=model.id)
    option.evidence = [RecommendationEvidence(benchmark_result_id=result.id)]
    project = Project(
        user_id=user.id, name="my-service", selected_option_id=None, baseline_model_id=model.id
    )
    db_session.add_all([option, project])
    db_session.commit()
    result_id, profile_id, option_id = result.id, profile.id, option.id

    # Read back through relationships (fresh identity map).
    db_session.expunge_all()
    fetched = db_session.get(BenchmarkResult, result_id)
    assert fetched.model.name == "Claude Haiku"
    assert fetched.benchmark.task_type == "agentic_coding"
    assert fetched.source_document.content_hash == "abc123"

    fetched_profile = db_session.get(RequirementsProfile, profile_id)
    assert fetched_profile.task_types == ["agentic_coding", "long_context"]
    assert fetched_profile.budget_sensitivity == "high"

    fetched_option = db_session.get(RecommendationOption, option_id)
    assert [e.benchmark_result_id for e in fetched_option.evidence] == [result.id]
    assert fetched_option.evidence[0].benchmark_result.metric == "pass@1_percent"


def test_recommendation_evidence_rejects_dangling_fk(db_session):
    """The join table is FK-enforced (the S2 review ask) — bad refs are rejected."""
    user = User(email="a@b.test", password_hash="h")
    model = Model(name="m", vendor="v")
    db_session.add_all([user, model])
    db_session.flush()
    profile = RequirementsProfile(user_id=user.id)
    db_session.add(profile)
    db_session.flush()
    option = RecommendationOption(profile_id=profile.id, model_id=model.id)
    db_session.add(option)
    db_session.flush()

    db_session.add(
        RecommendationEvidence(recommendation_option_id=option.id, benchmark_result_id=999999)
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_autogenerate_detects_no_drift(db_session):
    """The strong parity guard: Alembic autogenerate against the head DB finds
    NOTHING to change — no added/dropped tables or columns, and (compare_type +
    compare_server_default on) no type/nullable/default drift. This is the
    programmatic form of `alembic revision --autogenerate`, but it returns the
    diff in-memory so no probe migration file is ever written."""
    context = MigrationContext.configure(
        db_session.connection(),
        opts={
            "target_metadata": Base.metadata,
            "compare_type": True,
            "compare_server_default": True,
        },
    )
    diffs = compare_metadata(context, Base.metadata)
    assert diffs == [], f"ORM drift vs migrated schema: {diffs}"


def test_orm_tables_and_columns_match_migrated_schema(db_session):
    """Structural parity: ORM metadata == migrated DB, table-by-table, column-by-column."""
    inspector = inspect(db_session.get_bind())
    db_tables = set(inspector.get_table_names()) - {"alembic_version"}
    orm_tables = set(Base.metadata.tables)

    assert orm_tables == db_tables, (
        f"table drift — only in ORM: {sorted(orm_tables - db_tables)}; "
        f"only in DB: {sorted(db_tables - orm_tables)}"
    )

    for table in sorted(orm_tables):
        orm_cols = set(Base.metadata.tables[table].columns.keys())
        db_cols = {c["name"] for c in inspector.get_columns(table)}
        assert orm_cols == db_cols, (
            f"column drift in {table!r} — only in ORM: {sorted(orm_cols - db_cols)}; "
            f"only in DB: {sorted(db_cols - orm_cols)}"
        )
