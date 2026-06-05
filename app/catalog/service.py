"""Catalog store: turn denormalized rows into normalized, idempotent catalog data.

A catalog row (model/vendor + benchmark + optional harness + the metric figures)
is split across the dimension tables (get-or-create) and the benchmark_result
fact (upsert). Re-loading the same row is a no-op on identity and refreshes the
mutable figures — the idempotency the seed and S5b ingestion rely on.

The LLM never runs here: this is plain SQL. The LLM only *fills* the catalog
(S5b ingestion); ranking over it stays deterministic (S6).
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Benchmark, BenchmarkResult, Harness, Model
from app.schemas.catalog import CatalogRowIn, CatalogRowOut


def get_or_create_model(db: Session, name: str, vendor: str) -> Model:
    obj = db.scalar(select(Model).where(Model.name == name, Model.vendor == vendor))
    if obj is None:
        obj = Model(name=name, vendor=vendor)
        db.add(obj)
        db.flush()
    return obj


def get_or_create_benchmark(db: Session, name: str, task_type: str | None) -> Benchmark:
    obj = db.scalar(select(Benchmark).where(Benchmark.name == name))
    if obj is None:
        obj = Benchmark(name=name, task_type=task_type)
        db.add(obj)
        db.flush()
    return obj


def get_or_create_harness(
    db: Session, name: str | None, vendor: str | None
) -> Harness | None:
    if name is None:
        return None
    # (name, vendor) natural key; `== None` becomes `IS NULL` in SQLAlchemy.
    obj = db.scalar(
        select(Harness).where(Harness.name == name, Harness.vendor == vendor)
    )
    if obj is None:
        obj = Harness(name=name, vendor=vendor)
        db.add(obj)
        db.flush()
    return obj


def upsert_catalog_row(db: Session, row: CatalogRowIn) -> CatalogRowOut:
    """Idempotent insert-or-update of one catalog row; returns the stored row."""
    model = get_or_create_model(db, row.model, row.vendor)
    benchmark = get_or_create_benchmark(db, row.benchmark, row.task_type)
    harness = get_or_create_harness(db, row.harness, row.harness_vendor)

    mutable = {
        "task_type": row.task_type,
        "score": row.score,
        "cost_per_mtok": row.cost_per_mtok,
        "context_window": row.context_window,
        "source": row.source,
        "measured_at": row.measured_at,
    }
    stmt = pg_insert(BenchmarkResult).values(
        model_id=model.id,
        benchmark_id=benchmark.id,
        harness_id=harness.id if harness else None,
        metric=row.metric,
        **mutable,
    )
    # ON CONFLICT on the natural-key constraint → refresh the mutable figures only.
    stmt = stmt.on_conflict_do_update(
        constraint="uq_benchmark_result_identity",
        set_={k: stmt.excluded[k] for k in mutable},
    ).returning(BenchmarkResult.id)
    result_id = db.execute(stmt).scalar_one()
    db.commit()

    return CatalogRowOut(
        id=result_id,
        model=model.name,
        vendor=model.vendor,
        benchmark=benchmark.name,
        harness=harness.name if harness else None,
        harness_vendor=harness.vendor if harness else None,
        metric=row.metric,
        **mutable,
    )


def list_catalog(db: Session) -> list[CatalogRowOut]:
    """All catalog rows, denormalized back to names for the API/recommender."""
    results = db.scalars(
        select(BenchmarkResult).order_by(BenchmarkResult.id)
    ).all()
    rows: list[CatalogRowOut] = []
    for r in results:
        rows.append(
            CatalogRowOut(
                id=r.id,
                model=r.model.name,
                vendor=r.model.vendor,
                benchmark=r.benchmark.name,
                harness=r.harness.name if r.harness else None,
                harness_vendor=r.harness.vendor if r.harness else None,
                metric=r.metric,
                task_type=r.task_type,
                score=r.score,
                cost_per_mtok=r.cost_per_mtok,
                context_window=r.context_window,
                source=r.source,
                measured_at=r.measured_at,
            )
        )
    return rows
