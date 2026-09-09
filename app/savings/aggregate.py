"""Savings dashboard aggregate (S14): assemble `{ kpis, series, runs }` from runs.

Two layers, same shape as S12/S13:
- The PURE core (`assemble`) — a list of `RunRecord`s + threshold + a reference time
  → the `SavingsResponse` DTO. No DB, no LLM, zero tokens; unit-tested with known
  numbers. It reuses the honest-savings math (S12/S13) verbatim — the headline is
  `honest_cumulative_savings` (Σ savings over `quality_ok IS TRUE`), sub-threshold
  runs surfaced as quality risk, unrated counted separately (architecture §8).
- The wiring (`app/savings/dashboard.py`) — owner-scopes + loads the rows, then calls
  this. Kept separate so the assembly is testable without a database.

DETERMINISTIC — pure aggregation over already-computed per-run figures. Same inputs →
same output (the reference `now` is passed in, never read from the clock here).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional

from app.quality.service import acceptance_rate
from app.savings.service import _money, honest_cumulative_savings
from app.schemas.savings import (
    SavingsKpis,
    SavingsResponse,
    SavingsRunRow,
    SavingsSeriesPoint,
)

_ZERO = Decimal("0")
_MONTH_DAYS = Decimal("30")
_SECONDS_PER_DAY = Decimal("86400")


@dataclass(frozen=True)
class RunRecord:
    """One CI run's dashboard-relevant figures (already computed by S12/S13).

    `verdicts` are the accept/reject strings on this run's findings (S13); the per-run
    + overall acceptance rates are derived from them via the same `acceptance_rate`.
    """

    id: int
    created_at: datetime
    jenkins_build_id: Optional[str] = None
    model: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    cache_read_tokens: Optional[int] = None  # Reported separately; excluded from costs.
    actual_cost: Optional[Decimal] = None
    baseline_cost: Optional[Decimal] = None
    savings: Optional[Decimal] = None
    quality_ok: Optional[bool] = None
    gate: Optional[str] = None
    findings_count: int = 0
    verdicts: tuple[str, ...] = field(default_factory=tuple)
    cwes: tuple[str, ...] = field(default_factory=tuple)  # E20: distinct CWE ids


def _sum(values) -> Decimal:
    """Σ over an iterable of `Decimal | None`, treating None as 0 (unpriced run)."""
    return sum((v for v in values if v is not None), _ZERO)


def _project_monthly(amount: Decimal, span_days: Decimal) -> Optional[Decimal]:
    """Linear monthly projection of `amount` accrued over `span_days`, or None.

    Honest guard: with < 1 day of history the daily rate is undefined / wildly
    unstable, so we project nothing (the FE shows "—") rather than a fabricated figure.
    """
    if span_days < 1:
        return None
    return _money(amount / span_days * _MONTH_DAYS)


def assemble(
    records: list[RunRecord],
    threshold: float,
    now: datetime,
    range_label: str = "all",
    selected_model: Optional[str] = None,
    baseline_model: Optional[str] = None,
    task_type: Optional[str] = None,
) -> SavingsResponse:
    """Build the dashboard DTO from per-run records (pure; no DB, no LLM).

    `records` must be ordered oldest → newest (the caller orders by `created_at`, id).
    `now` is the reference time for the monthly projection — passed in so the result
    is deterministic and testable. `threshold` is `QUALITY_THRESHOLD` (env-driven).
    `selected_model` / `baseline_model` are the names the chart legend renders.
    """
    runs_sorted = sorted(records, key=lambda r: (r.created_at, r.id))

    # Headline honesty (S12/S13): banked-only cumulative + surfaced quality risk.
    honest = honest_cumulative_savings(
        [(r.savings, r.quality_ok) for r in runs_sorted]
    )

    # The % denominator: what the baseline WOULD have cost on the banked runs only.
    baseline_total = _sum(
        r.baseline_cost for r in runs_sorted if r.quality_ok is True
    )
    saved_pct = (
        float(honest.cumulative / baseline_total * 100)
        if baseline_total > 0
        else None
    )

    spend_this_period = _sum(r.actual_cost for r in runs_sorted)

    # Overall quality = accepted / rated across every finding in the window.
    all_verdicts = [v for r in runs_sorted for v in r.verdicts]
    overall_rate = acceptance_rate(all_verdicts)
    if overall_rate is None:
        quality_status = "unrated"
    elif overall_rate >= threshold:
        quality_status = "banking"
    else:
        quality_status = "quality_risk"

    # Linear monthly projection over the observed span (first run → now).
    if runs_sorted:
        span_days = Decimal(
            (now - runs_sorted[0].created_at).total_seconds()
        ) / _SECONDS_PER_DAY
        projected_spend = _project_monthly(spend_this_period, span_days)
        projected_savings = _project_monthly(honest.cumulative, span_days)
    else:
        projected_spend = projected_savings = None

    kpis = SavingsKpis(
        cumulative_saved=honest.cumulative,
        saved_pct=saved_pct,
        baseline_total=baseline_total,
        spend_this_period=spend_this_period,
        quality_risk=honest.quality_risk,
        projected_monthly_spend=projected_spend,
        projected_monthly_savings=projected_savings,
        acceptance_rate=overall_rate,
        quality_status=quality_status,
        threshold=threshold,
        runs_count=len(runs_sorted),
        banked_runs=honest.banked_runs,
        quality_risk_runs=honest.quality_risk_runs,
        unrated_runs=honest.unrated_runs,
    )

    series = [
        SavingsSeriesPoint(
            date=r.created_at,
            jenkins_build_id=r.jenkins_build_id,
            actual=r.actual_cost,
            baseline=r.baseline_cost,
            savings=r.savings,
            quality_ok=r.quality_ok,
            acceptance_rate=acceptance_rate(r.verdicts),
        )
        for r in runs_sorted
    ]

    runs = [
        SavingsRunRow(
            id=r.id,
            jenkins_build_id=r.jenkins_build_id,
            created_at=r.created_at,
            model=r.model,
            tokens_in=r.tokens_in,
            tokens_out=r.tokens_out,
            cache_read_tokens=r.cache_read_tokens,
            actual_cost=r.actual_cost,
            baseline_cost=r.baseline_cost,
            savings=r.savings,
            quality_ok=r.quality_ok,
            acceptance_rate=acceptance_rate(r.verdicts),
            gate=r.gate,
            findings_count=r.findings_count,
            cwes=list(r.cwes),
        )
        for r in runs_sorted
    ]

    return SavingsResponse(
        range=range_label,
        selected_model=selected_model,
        baseline_model=baseline_model,
        task_type=task_type,
        kpis=kpis,
        series=series,
        runs=runs,
    )
