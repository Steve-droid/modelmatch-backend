"""Deterministic validation of UNTRUSTED LLM ingestion output (ABC's lesson).

The model's JSON is never trusted: we strip a markdown fence, parse strictly, and
validate each row into `IngestedCatalogRow` with sanity bounds (incl. the DB
column ranges, so a hostile value can't overflow Postgres). Invalid rows are
DROPPED with a recorded reason — never a second LLM call, never a guess. One bad
row never sinks the whole ingest; the valid rows still upsert.
"""

from __future__ import annotations

import json
from decimal import Decimal

from pydantic import Field, ValidationError

from app.schemas.catalog import CatalogRowIn
from app.schemas.ingest import IngestRejection

# Column-aligned bounds (app/models/orm.py): strings ≤ their column widths; score
# fits Numeric(8,4) (max 9999.9999); cost fits Numeric(14,6); context_window fits a
# 32-bit Integer. These are the hard ceilings that keep untrusted input from
# erroring the upsert; the metric semantics (e.g. a percentage) stay open by design.
_MAX_SCORE = Decimal("9999.9999")
_MAX_COST = Decimal("99999999.999999")
_MAX_CONTEXT_WINDOW = 2_147_483_647  # PG Integer max
_ENVELOPE_INDEX = -1  # rejection index for a whole-output failure (not a single row)


class IngestedCatalogRow(CatalogRowIn):
    """A CatalogRowIn with the strict bounds enforced on untrusted model output:
    non-empty, length-capped strings and non-negative, column-bounded numerics."""

    model: str = Field(min_length=1, max_length=200)
    vendor: str = Field(min_length=1, max_length=100)
    benchmark: str = Field(min_length=1, max_length=200)
    metric: str = Field(min_length=1, max_length=64)
    score: Decimal = Field(ge=0, le=_MAX_SCORE)
    cost_per_mtok: Decimal = Field(ge=0, le=_MAX_COST)
    harness: str | None = Field(default=None, max_length=200)
    harness_vendor: str | None = Field(default=None, max_length=100)
    task_type: str | None = Field(default=None, max_length=64)
    context_window: int | None = Field(default=None, ge=0, le=_MAX_CONTEXT_WINDOW)
    source: str | None = Field(default=None, max_length=1024)


def strip_code_fence(text: str) -> str:
    """Unwrap a ```json … ``` (or bare ```) fence many models emit around JSON.

    Deterministic and safe: anything that still isn't JSON is rejected downstream.
    (Mirrors the agent's reviewer; kept local so app/ingest doesn't import agent/.)
    """
    t = text.strip()
    if not t.startswith("```"):
        return t
    t = t[3:]
    newline = t.find("\n")
    if newline != -1 and t[:newline].strip().isalpha():  # optional language tag
        t = t[newline + 1 :]
    t = t.rstrip()
    if t.endswith("```"):
        t = t[:-3]
    return t.strip()


def _short_reason(exc: ValidationError) -> str:
    """Compress a Pydantic error into one human-readable line for the rejection log."""
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err.get("loc", ())) or "(row)"
    return f"{loc}: {err.get('msg', 'invalid')}"


def parse_catalog_rows(
    text: str,
) -> tuple[list[IngestedCatalogRow], list[IngestRejection]]:
    """Parse the model's output into (valid rows, rejections).

    Never raises: an unparseable envelope (non-JSON, or no `rows` array) yields zero
    valid rows plus one envelope-level rejection; per-row failures yield a rejection
    each. The caller decides status (no valid rows → 'invalid').
    """
    try:
        data = json.loads(strip_code_fence(text))
    except json.JSONDecodeError as exc:
        return [], [IngestRejection(index=_ENVELOPE_INDEX, reason=f"not valid JSON: {exc}")]

    raw = data.get("rows") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return [], [IngestRejection(index=_ENVELOPE_INDEX, reason="expected a 'rows' array")]

    valid: list[IngestedCatalogRow] = []
    rejections: list[IngestRejection] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            rejections.append(IngestRejection(index=i, reason="row is not an object"))
            continue
        try:
            valid.append(IngestedCatalogRow.model_validate(item))
        except ValidationError as exc:
            rejections.append(IngestRejection(index=i, reason=_short_reason(exc)))
    return valid, rejections
