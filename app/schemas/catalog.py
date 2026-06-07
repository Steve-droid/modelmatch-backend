"""Catalog contract schemas (used from S5 / S5b).

These ride the deterministic recommender's data; `task_type` / `metric` are open
strings (the ingestion path #3 grows them), matching the schema decision in S2.
Field names are snake_case in code and serialize as camelCase on the wire.
"""

from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from app.schemas.base import CamelModel

DataPolicy = Literal["trains_on_input", "private"]


class ModelCreate(CamelModel):
    name: str
    vendor: str
    # price_per_mtok = LEGACY blended display/ranking price. input/output = S12 split
    # pricing the savings engine uses (real APIs price input vs output differently).
    price_per_mtok: Optional[Decimal] = None
    input_price_per_mtok: Optional[Decimal] = None
    output_price_per_mtok: Optional[Decimal] = None
    data_policy: Optional[DataPolicy] = None


class ModelOut(CamelModel):
    id: int
    name: str
    vendor: str
    price_per_mtok: Optional[Decimal] = None
    input_price_per_mtok: Optional[Decimal] = None
    output_price_per_mtok: Optional[Decimal] = None
    data_policy: Optional[DataPolicy] = None


class HarnessOut(CamelModel):
    id: int
    name: str
    vendor: Optional[str] = None


class BenchmarkOut(CamelModel):
    id: int
    name: str
    task_type: Optional[str] = None


class BenchmarkResultCreate(CamelModel):
    model_id: int
    benchmark_id: int
    harness_id: Optional[int] = None
    task_type: Optional[str] = None
    score: Optional[Decimal] = None
    metric: Optional[str] = None
    cost_per_mtok: Optional[Decimal] = None
    context_window: Optional[int] = None
    source: Optional[str] = None
    source_document_id: Optional[int] = None
    measured_at: Optional[date] = None


class BenchmarkResultOut(BenchmarkResultCreate):
    id: int


class CatalogRowIn(CamelModel):
    """A denormalized catalog row (what /benchmarks and the seed accept).

    The service resolves the name fields to model/benchmark/harness rows
    (get-or-create) and upserts the benchmark_result. Mirrors the seed format.
    """

    model: str
    vendor: str
    benchmark: str
    metric: str
    score: Decimal
    # cost_per_mtok = the legacy blended price (recommender ranking + display); REQUIRED.
    # The split input/output prices (S12 savings) are OPTIONAL — when absent the upsert
    # backfills both from cost_per_mtok, so old/partial rows stay costable.
    cost_per_mtok: Decimal
    input_price_per_mtok: Optional[Decimal] = None
    output_price_per_mtok: Optional[Decimal] = None
    harness: Optional[str] = None
    harness_vendor: Optional[str] = None
    task_type: Optional[str] = None
    context_window: Optional[int] = None
    source: Optional[str] = None
    measured_at: Optional[date] = None


class CatalogRowOut(CatalogRowIn):
    id: int  # benchmark_result id
