"""Catalog store: turn denormalized rows into normalized, idempotent catalog data.

A catalog row (model/vendor + benchmark + optional harness + the metric figures)
is split across the dimension tables (get-or-create) and the benchmark_result
fact (upsert). Re-loading the same row is a no-op on identity and refreshes the
mutable figures — the idempotency the seed and S5b ingestion rely on.

The LLM never runs here: this is plain SQL. The LLM only *fills* the catalog
(S5b ingestion); ranking over it stays deterministic (S6).
"""

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Benchmark, BenchmarkResult, Harness, Model
from app.schemas.catalog import CatalogRowIn, CatalogRowOut

# Ranking/display cost is blended 3:1 (input:output) — a code-review workload reads a
# large diff and emits compact findings. Quantized to the cost column's 6-dp scale.
_BLEND_QUANT = Decimal("0.000001")


def _ranking_cost(row: CatalogRowIn) -> Decimal | None:
    """The deterministic ranking/display `cost_per_mtok` for a row.

    When BOTH split prices are present we DERIVE it as `(3*input + output)/4` — the
    same formula the seed documents — so seeded and ingested rows share one cost basis
    and the LLM never does ranking arithmetic. Otherwise we fall back to the row's
    provided blended `cost_per_mtok`.
    """
    if row.input_price_per_mtok is not None and row.output_price_per_mtok is not None:
        blended = (
            Decimal(3) * row.input_price_per_mtok + row.output_price_per_mtok
        ) / Decimal(4)
        return blended.quantize(_BLEND_QUANT, rounding=ROUND_HALF_UP)
    return row.cost_per_mtok


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


def upsert_catalog_row(
    db: Session, row: CatalogRowIn, *, source_document_id: int | None = None
) -> CatalogRowOut:
    """Idempotent insert-or-update of one catalog row; returns the stored row.

    `source_document_id` (S5b ingestion) links the row to the source it came from.
    It's NOT part of the row's identity, so two sources stating the same figure still
    dedupe to one row — the latest ingest's provenance wins. A provenance-less upsert
    (seed / `POST /benchmarks`, id=None) never clears an existing link.
    """
    model = get_or_create_model(db, row.model, row.vendor)
    # Ranking/display blended price (NOT read by the S12 savings engine). DERIVED from
    # the split prices when present (never trusted from the LLM), so seed + ingested
    # rows rank on the same basis. Last-write-wins; the `!=` guard keeps an unchanged
    # re-upsert (seed/ingestion idempotency) from dirtying the row.
    ranking_cost = _ranking_cost(row)
    if ranking_cost is not None and model.price_per_mtok != ranking_cost:
        model.price_per_mtok = ranking_cost
    # S12 split pricing (authoritative for savings): use the row's explicit input/output
    # prices when present; otherwise BACKFILL both from the legacy blended cost_per_mtok
    # so old/partial rows stay costable (input==output==blended reproduces the old math).
    in_price = row.input_price_per_mtok if row.input_price_per_mtok is not None else row.cost_per_mtok
    out_price = row.output_price_per_mtok if row.output_price_per_mtok is not None else row.cost_per_mtok
    if in_price is not None and model.input_price_per_mtok != in_price:
        model.input_price_per_mtok = in_price
    if out_price is not None and model.output_price_per_mtok != out_price:
        model.output_price_per_mtok = out_price
    benchmark = get_or_create_benchmark(db, row.benchmark, row.task_type)
    harness = get_or_create_harness(db, row.harness, row.harness_vendor)

    mutable = {
        "task_type": row.task_type,
        "score": row.score,
        "cost_per_mtok": ranking_cost,  # derived 3:1 blend, not the raw row value
        "context_window": row.context_window,
        "source": row.source,
        "measured_at": row.measured_at,
    }
    stmt = pg_insert(BenchmarkResult).values(
        model_id=model.id,
        benchmark_id=benchmark.id,
        harness_id=harness.id if harness else None,
        metric=row.metric,
        source_document_id=source_document_id,
        **mutable,
    )
    # ON CONFLICT on the natural-key constraint → refresh the mutable figures only.
    # Provenance is refreshed too, but ONLY when this upsert carries one (else a
    # seed/API re-upsert would null out an earlier ingestion's source link).
    set_cols = {k: stmt.excluded[k] for k in mutable}
    if source_document_id is not None:
        set_cols["source_document_id"] = stmt.excluded["source_document_id"]
    stmt = stmt.on_conflict_do_update(
        constraint="uq_benchmark_result_identity",
        set_=set_cols,
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
        input_price_per_mtok=model.input_price_per_mtok,
        output_price_per_mtok=model.output_price_per_mtok,
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
                input_price_per_mtok=r.model.input_price_per_mtok,
                output_price_per_mtok=r.model.output_price_per_mtok,
                context_window=r.context_window,
                source=r.source,
                measured_at=r.measured_at,
            )
        )
    return rows
