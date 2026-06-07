"""SQLAlchemy 2.0 ORM models — one per table in the initial migration (S2).

These mirror the migration exactly (table/column names, types, nullability, FKs,
enums). The migration is the source of truth for DDL; these give the app typed,
relational access and feed Alembic autogenerate. The parity test in
tests/test_orm_models.py fails if a model drifts from the migrated schema.

Enums reference the existing PG types by name with create_type=False — the
migration already created them, so the ORM must not try to (re)create or drop them.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

# Native PG enum types — same names as the migration, create_type=False so the
# ORM neither creates nor drops them (the migration owns their lifecycle).
DATA_POLICY = ENUM("trains_on_input", "private", name="data_policy", create_type=False)
BUDGET_SENSITIVITY = ENUM("low", "medium", "high", name="budget_sensitivity", create_type=False)
FINDING_CATEGORY = ENUM("security", "style", name="finding_category", create_type=False)
FEEDBACK_VERDICT = ENUM("accept", "reject", name="feedback_verdict", create_type=False)
CHAT_ROLE = ENUM("user", "assistant", name="chat_role", create_type=False)
TRACE_KIND = ENUM("savings", "benchmark_result", name="trace_kind", create_type=False)
LLM_PURPOSE = ENUM("ingestion", "chat", "agent", name="llm_purpose", create_type=False)
ALERT_KIND = ENUM("upgrade", "downgrade", name="alert_kind", create_type=False)

# Shared numeric shapes (match the migration's _money / _score).
_MONEY = Numeric(14, 6)
_SCORE = Numeric(8, 4)


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))


class Model(Base):
    __tablename__ = "model"
    __table_args__ = (UniqueConstraint("name", "vendor", name="uq_model_name_vendor"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    vendor: Mapped[str] = mapped_column(String(100))
    # LEGACY blended catalog-display / recommender-ranking price. NOT used by the S12
    # savings engine (real LLM APIs price input vs output differently — see the split
    # columns below). Kept for backward compatibility + a single display figure.
    price_per_mtok: Mapped[Optional[Decimal]] = mapped_column(_MONEY)
    # S12 split pricing (authoritative for savings): real per-MTok input vs output
    # rates. Populated by the catalog upsert (explicit, or backfilled from the legacy
    # blended price for old/partial rows). NULL when a model is unpriced.
    input_price_per_mtok: Mapped[Optional[Decimal]] = mapped_column(_MONEY)
    output_price_per_mtok: Mapped[Optional[Decimal]] = mapped_column(_MONEY)
    data_policy: Mapped[Optional[str]] = mapped_column(DATA_POLICY)


class Harness(Base):
    __tablename__ = "harness"
    __table_args__ = (
        # Natural key (name, vendor), matching model's style — two vendors may ship
        # a harness of the same name. NULLS NOT DISTINCT so a vendor-less harness
        # ("SWE-agent" with no vendor) still dedupes on re-ingest.
        UniqueConstraint(
            "name",
            "vendor",
            name="uq_harness_name_vendor",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    vendor: Mapped[Optional[str]] = mapped_column(String(100))


class Benchmark(Base):
    __tablename__ = "benchmark"
    __table_args__ = (UniqueConstraint("name", name="uq_benchmark_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    task_type: Mapped[Optional[str]] = mapped_column(String(64))


class SourceDocument(Base):
    __tablename__ = "source_document"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[Optional[str]] = mapped_column(String(64))
    uri: Mapped[Optional[str]] = mapped_column(String(1024))
    s3_key: Mapped[Optional[str]] = mapped_column(String(1024))
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[Optional[str]] = mapped_column(String(32))


class BenchmarkResult(Base):
    __tablename__ = "benchmark_result"
    __table_args__ = (
        # Idempotency key for ingestion/seed: one row per (model, benchmark,
        # harness, metric). NULLS NOT DISTINCT (PG15+) so a NULL harness still
        # dedupes — otherwise two harness-less rows for the same model/benchmark/
        # metric would both be allowed.
        UniqueConstraint(
            "model_id",
            "benchmark_id",
            "harness_id",
            "metric",
            name="uq_benchmark_result_identity",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("model.id"))
    harness_id: Mapped[Optional[int]] = mapped_column(ForeignKey("harness.id"))
    benchmark_id: Mapped[int] = mapped_column(ForeignKey("benchmark.id"))
    task_type: Mapped[Optional[str]] = mapped_column(String(64))
    score: Mapped[Optional[Decimal]] = mapped_column(_SCORE)
    metric: Mapped[Optional[str]] = mapped_column(String(64))
    cost_per_mtok: Mapped[Optional[Decimal]] = mapped_column(_MONEY)
    context_window: Mapped[Optional[int]] = mapped_column(Integer)
    source: Mapped[Optional[str]] = mapped_column(String(1024))
    source_document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("source_document.id"))
    measured_at: Mapped[Optional[date]] = mapped_column(Date)

    model: Mapped[Model] = relationship()
    harness: Mapped[Optional[Harness]] = relationship()
    benchmark: Mapped[Benchmark] = relationship()
    source_document: Mapped[Optional[SourceDocument]] = relationship()


class RequirementsProfile(Base):
    __tablename__ = "requirements_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    task_types: Mapped[Optional[list[str]]] = mapped_column(ARRAY(String(64)))
    budget_sensitivity: Mapped[Optional[str]] = mapped_column(BUDGET_SENSITIVITY)
    latency_need: Mapped[Optional[str]] = mapped_column(String(32))


class RecommendationOption(Base):
    __tablename__ = "recommendation_option"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("requirements_profile.id"))
    rank: Mapped[Optional[int]] = mapped_column(Integer)
    rank_score: Mapped[Optional[Decimal]] = mapped_column(_MONEY)
    model_id: Mapped[int] = mapped_column(ForeignKey("model.id"))
    harness_id: Mapped[Optional[int]] = mapped_column(ForeignKey("harness.id"))

    profile: Mapped[RequirementsProfile] = relationship()
    evidence: Mapped[list[RecommendationEvidence]] = relationship(
        back_populates="option", cascade="all, delete-orphan"
    )


class RecommendationEvidence(Base):
    __tablename__ = "recommendation_evidence"
    __table_args__ = (
        PrimaryKeyConstraint(
            "recommendation_option_id",
            "benchmark_result_id",
            name="pk_recommendation_evidence",
        ),
    )

    recommendation_option_id: Mapped[int] = mapped_column(
        ForeignKey("recommendation_option.id", ondelete="CASCADE")
    )
    benchmark_result_id: Mapped[int] = mapped_column(ForeignKey("benchmark_result.id"))

    option: Mapped[RecommendationOption] = relationship(back_populates="evidence")
    benchmark_result: Mapped[BenchmarkResult] = relationship()


class Project(Base):
    __tablename__ = "project"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    name: Mapped[str] = mapped_column(String(200))
    selected_option_id: Mapped[Optional[int]] = mapped_column(ForeignKey("recommendation_option.id"))
    baseline_model_id: Mapped[Optional[int]] = mapped_column(ForeignKey("model.id"))

    user: Mapped[User] = relationship()


class JenkinsConnection(Base):
    __tablename__ = "jenkins_connection"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"), unique=True)
    base_url: Mapped[Optional[str]] = mapped_column(String(1024))
    job_name: Mapped[Optional[str]] = mapped_column(String(255))
    # secret-store references, NEVER plaintext (comments mirror the migration).
    jenkins_token_ref: Mapped[Optional[str]] = mapped_column(
        String(255),
        comment="secret-store reference to the Jenkins API token — NEVER plaintext",
    )
    model_api_key_ref: Mapped[Optional[str]] = mapped_column(
        String(255),
        comment="secret-store reference to the BYOK model key — NEVER plaintext",
    )
    # SHA-256 hex of the per-project CI ingest token — NEVER the plaintext token.
    # Minted once at GET /ci-setup; rotation is a later explicit endpoint.
    ci_token_hash: Mapped[Optional[str]] = mapped_column(
        String(64),
        comment="SHA-256 hash of the per-project CI ingest token — NEVER plaintext",
    )
    status: Mapped[Optional[str]] = mapped_column(String(32))

    project: Mapped[Project] = relationship()


class CiRun(Base):
    __tablename__ = "ci_run"
    __table_args__ = (
        # One run per (project, Jenkins build) — a re-POSTed build id is rejected
        # (409). NULL build ids stay distinct (non-ingest inserts may omit it).
        UniqueConstraint(
            "project_id", "jenkins_build_id", name="uq_ci_run_project_build"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"))
    jenkins_build_id: Mapped[Optional[str]] = mapped_column(String(255))
    model_id: Mapped[Optional[int]] = mapped_column(ForeignKey("model.id"))
    task: Mapped[str] = mapped_column(String(32), server_default="code_review")
    tokens_in: Mapped[Optional[int]] = mapped_column(Integer)
    tokens_out: Mapped[Optional[int]] = mapped_column(Integer)
    actual_cost: Mapped[Optional[Decimal]] = mapped_column(_MONEY)
    baseline_cost: Mapped[Optional[Decimal]] = mapped_column(_MONEY)
    savings: Mapped[Optional[Decimal]] = mapped_column(_MONEY)
    quality_ok: Mapped[Optional[bool]] = mapped_column(Boolean)
    # Audit trail of the agent's pass/fail decision (the gate acts in the user's
    # CI; we keep the record, like an ingestion run's status/errors). Not the S13
    # quality_ok gate — that's acceptance-rate based.
    gate: Mapped[Optional[str]] = mapped_column(
        String(16),
        comment="agent pass/fail audit trail (the gate acts in the user's CI)",
    )
    gate_reason: Mapped[Optional[str]] = mapped_column(Text)
    # When the run was ingested. The dashboard's time axis (S14): ci_run had no
    # timestamp, so the savings series/KPIs ("this period", projected monthly,
    # ?range) had nothing to order or bucket by. DB-assigned on insert
    # (server_default now()) — same pattern as chat_message.created_at.
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    project: Mapped[Project] = relationship()


class CiFinding(Base):
    __tablename__ = "ci_finding"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ci_run_id: Mapped[int] = mapped_column(ForeignKey("ci_run.id"))
    severity: Mapped[Optional[str]] = mapped_column(String(32))
    category: Mapped[Optional[str]] = mapped_column(FINDING_CATEGORY)
    file: Mapped[Optional[str]] = mapped_column(String(1024))
    line: Mapped[Optional[int]] = mapped_column(Integer)
    message: Mapped[Optional[str]] = mapped_column(Text)

    ci_run: Mapped[CiRun] = relationship()


class FindingFeedback(Base):
    __tablename__ = "finding_feedback"
    __table_args__ = (
        # One verdict per (finding, user) — the quality-gate invariant, enforced in
        # the DB (S13) so parallel POSTs across replicas can't double-insert (the
        # ON CONFLICT target for the upsert). NULLS NOT DISTINCT so a NULL user_id
        # still dedupes, matching the codebase's other natural keys.
        UniqueConstraint(
            "ci_finding_id",
            "user_id",
            name="uq_finding_feedback_finding_user",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ci_finding_id: Mapped[int] = mapped_column(ForeignKey("ci_finding.id"))
    verdict: Mapped[str] = mapped_column(FEEDBACK_VERDICT)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("user.id"))

    ci_finding: Mapped[CiFinding] = relationship()


class ChatMessage(Base):
    __tablename__ = "chat_message"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"))
    role: Mapped[str] = mapped_column(CHAT_ROLE)
    text: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    project: Mapped[Project] = relationship()


class RetrievalTrace(Base):
    __tablename__ = "retrieval_trace"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_message_id: Mapped[int] = mapped_column(ForeignKey("chat_message.id"))
    kind: Mapped[str] = mapped_column(TRACE_KIND)
    ref: Mapped[Optional[str]] = mapped_column(String(255))
    snippet: Mapped[Optional[str]] = mapped_column(Text)

    chat_message: Mapped[ChatMessage] = relationship()


class LlmCall(Base):
    __tablename__ = "llm_call"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ci_run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("ci_run.id"))
    purpose: Mapped[str] = mapped_column(LLM_PURPOSE)
    model_id: Mapped[Optional[int]] = mapped_column(ForeignKey("model.id"))
    tokens_in: Mapped[Optional[int]] = mapped_column(Integer)
    tokens_out: Mapped[Optional[int]] = mapped_column(Integer)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[Optional[str]] = mapped_column(String(32))


class ProactiveAlert(Base):
    __tablename__ = "proactive_alert"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"))
    kind: Mapped[str] = mapped_column(ALERT_KIND)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    evidence: Mapped[Optional[dict]] = mapped_column(JSONB)
    status: Mapped[Optional[str]] = mapped_column(String(32))


class LlmUsage(Base):
    __tablename__ = "llm_usage"

    # Replica-shared tally for the hard hourly token cap; hour_start is the PK.
    hour_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    tokens_used: Mapped[int] = mapped_column(BigInteger, server_default="0")
