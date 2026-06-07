"""Savings engine (S12): per-`ci_run` actual-vs-baseline cost → savings.

The product's headline number: "prove a cheaper model is good enough — and show the
money saved." For each CI run we know the token usage; the selected model's prices
give the *actual* cost, the project's baseline model's prices give what the expensive
default *would have* cost, and the gap is the savings.

DETERMINISTIC — pure arithmetic, NO LLM, zero tokens (same discipline as the
recommender). Same inputs → same output (unit-tested with known numbers).

Split pricing: real LLM APIs price input (prompt) and output (completion) tokens
differently, so we cost them separately:

    cost = tokens_in  / 1_000_000 * input_price_per_mtok
         + tokens_out / 1_000_000 * output_price_per_mtok

Money invariants:
- Everything is `Decimal`, never float (prices come off the DB as `Decimal`).
- Prices are per MILLION tokens, so divide by 1e6.
- Results are quantized to the `ci_run` money columns' scale (`Numeric(14,6)`),
  so the value we echo back equals the value Postgres stores.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple, Optional

from sqlalchemy.orm import Session

from app.models import Model

# Numeric(14,6) — the `_MONEY` shape used by ci_run.actual_cost/baseline_cost/savings.
_MONEY_QUANT = Decimal("0.000001")
_PER_MTOK = Decimal(1_000_000)  # prices are quoted per MILLION tokens


class Pricing(NamedTuple):
    """A model's split per-MTok prices — both required to cost a run."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal


def _money(value: Decimal) -> Decimal:
    """Round a Decimal to the money columns' 6-dp scale (half-up)."""
    return value.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)


def _cost(tokens_in: int, tokens_out: int, pricing: Pricing) -> Decimal:
    """Blend split input/output prices over the run's token split → cost (6 dp)."""
    raw = (
        Decimal(tokens_in) * pricing.input_per_mtok
        + Decimal(tokens_out) * pricing.output_per_mtok
    ) / _PER_MTOK
    return _money(raw)


def compute_savings(
    tokens_in: int,
    tokens_out: int,
    selected: Optional[Pricing],
    baseline: Optional[Pricing],
) -> tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
    """Pure savings math for one run → `(actual_cost, baseline_cost, savings)`.

    `actual` = the run's tokens costed at the SELECTED model's split prices;
    `baseline` = the same tokens at the project's BASELINE model's split prices;
    `savings = baseline − actual`. All `Decimal`, quantized to 6 dp. Savings may be
    negative (a pick pricier than the baseline — an honest overspend signal).

    If either model is unpriced (missing input OR output price → `None` Pricing), the
    run can't be costed: return `(None, None, None)`. The caller leaves the trio NULL
    and the dashboard shows the run without a savings figure — we don't fail ingest.
    """
    if selected is None or baseline is None:
        return None, None, None

    actual = _cost(tokens_in, tokens_out, selected)
    baseline_cost = _cost(tokens_in, tokens_out, baseline)
    savings = _money(baseline_cost - actual)
    return actual, baseline_cost, savings


class HonestSavings(NamedTuple):
    """The quality-gated savings breakdown (S13, architecture §8).

    The **headline** `cumulative` counts only quality-passing runs. Sub-threshold
    runs aren't silently dropped — their savings are summed into `quality_risk` and
    counted, so the dashboard can surface "you'd save this much more if quality held."
    Un-gated (unrated) runs are counted separately (`unrated_runs`); they bank nothing
    yet but aren't a quality failure either.
    """

    cumulative: Decimal       # Σ savings over runs where quality_ok IS TRUE — the honest headline
    banked_runs: int          # how many quality-passing runs contributed
    quality_risk: Decimal     # Σ savings over quality_ok IS FALSE (excluded but SURFACED)
    quality_risk_runs: int
    unrated_runs: int         # quality_ok IS NULL (un-gated — neither banked nor at risk)


def honest_cumulative_savings(
    runs: Iterable[tuple[Optional[Decimal], Optional[bool]]],
) -> HonestSavings:
    """Aggregate per-run `(savings, quality_ok)` into the honest cumulative (§8).

    Only `quality_ok IS TRUE` runs count toward `cumulative` (the headline). Failing
    runs (`quality_ok IS FALSE`) are EXCLUDED from the headline but their savings are
    tallied into `quality_risk` so they stay visible — never silently dropped. NULL
    (un-gated) runs are counted separately. A run with NULL savings (unpriced model)
    contributes 0 to the money totals but still counts toward its run tally.

    Pure arithmetic, all `Decimal` — no DB, no LLM.
    """
    cumulative = Decimal("0")
    quality_risk = Decimal("0")
    banked_runs = quality_risk_runs = unrated_runs = 0

    for savings, quality_ok in runs:
        amount = savings if savings is not None else Decimal("0")
        if quality_ok is True:
            cumulative += amount
            banked_runs += 1
        elif quality_ok is False:
            quality_risk += amount
            quality_risk_runs += 1
        else:  # None → un-gated (unrated)
            unrated_runs += 1

    return HonestSavings(
        cumulative=_money(cumulative),
        banked_runs=banked_runs,
        quality_risk=_money(quality_risk),
        quality_risk_runs=quality_risk_runs,
        unrated_runs=unrated_runs,
    )


def price_for(db: Session, model_id: Optional[int]) -> Optional[Pricing]:
    """A model id's split `Pricing`, or None if unknown/unpriced.

    Both `input_price_per_mtok` and `output_price_per_mtok` are required — if either is
    NULL the model is "unpriced" and savings can't be computed. The catalog upsert
    populates both (explicit, or backfilled from the legacy blended price). None when
    the model id is absent (no selected option yet) or the model row is missing.
    """
    if model_id is None:
        return None
    model = db.get(Model, model_id)
    if model is None:
        return None
    if model.input_price_per_mtok is None or model.output_price_per_mtok is None:
        return None
    return Pricing(model.input_price_per_mtok, model.output_price_per_mtok)
