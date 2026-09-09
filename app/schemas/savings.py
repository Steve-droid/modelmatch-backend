"""Savings dashboard contract (S14): `GET /projects/{id}/savings` → `{ kpis, series, runs }`.

A pure READ over the `ci_run` rows the savings engine (S12) + quality gate (S13)
already computed — no LLM, no new math beyond aggregation. The honesty story is made
visual here (architecture §4.1, §8): the headline KPI banks ONLY quality-passing runs;
sub-threshold runs are EXCLUDED from it but SURFACED as "quality risk"; unrated runs
(no feedback yet) are a third, distinct state. Never silently dropped.

camelCase out (CamelModel). Money is `Decimal` server-side; FastAPI serializes it as a
JSON number (tests compare via `Decimal(str(...))`, like S12/S13).
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from app.schemas.base import CamelModel

# The dashboard's quality summary state (drives the Quality KPI card's colour):
# banking = acceptance rate ≥ threshold; quality_risk = below; unrated = nothing rated.
QualityStatus = Literal["banking", "quality_risk", "unrated"]

# `?range=` window. Validated as a Literal so a bad value is a clean 422 (architecture
# §6 errors). "all" = whole history (the default; the demo rarely needs more).
SavingsRange = Literal["all", "7d", "30d", "90d"]


class SavingsKpis(CamelModel):
    """The top-row KPI cards (architecture §4.1).

    `cumulative_saved` is the honest headline — `honest_cumulative_savings.cumulative`
    (Σ savings over `quality_ok IS TRUE` only). `quality_risk` is the savings the
    sub-threshold runs WOULD have banked — surfaced, not dropped. `saved_pct` is the
    headline as a fraction of what the baseline would have cost on the banked runs.
    """

    cumulative_saved: Decimal              # banked headline ($)
    saved_pct: Optional[float] = None      # cumulative / baseline_total_banked * 100; None if no baseline
    baseline_total: Decimal                # Σ baseline_cost over banked runs (the % denominator + chart context)
    spend_this_period: Decimal             # Σ actual_cost over ALL runs in range (what you actually spent)
    quality_risk: Decimal                  # Σ savings over quality_ok IS FALSE (excluded from headline, surfaced)
    projected_monthly_spend: Optional[Decimal] = None    # linear projection; None if too little time to project
    projected_monthly_savings: Optional[Decimal] = None
    acceptance_rate: Optional[float] = None  # overall accepted/rated across the period; None if nothing rated
    quality_status: QualityStatus
    threshold: float                       # QUALITY_THRESHOLD (env-driven; the trend chart's reference line)
    runs_count: int
    banked_runs: int
    quality_risk_runs: int
    unrated_runs: int


class SavingsSeriesPoint(CamelModel):
    """One point on the time-series charts — one CI run, ordered by `date` (S14 cut:
    per-run points, not date buckets; `created_at` is the real time axis). Feeds the
    actual-vs-baseline area (shaded gap = `savings`), the cost/run bar (coloured by
    `quality_ok`), and the quality trend (`acceptance_rate` vs the threshold line)."""

    date: datetime
    jenkins_build_id: Optional[str] = None  # so a chart tooltip can name the run
    actual: Optional[Decimal] = None
    baseline: Optional[Decimal] = None
    savings: Optional[Decimal] = None
    quality_ok: Optional[bool] = None
    acceptance_rate: Optional[float] = None


class SavingsRunRow(CamelModel):
    """A row in the runs table (architecture §4.1): build · date · model · tokens ·
    actual · baseline · savings · quality_ok · #findings → drill into the findings."""

    id: int
    jenkins_build_id: Optional[str] = None
    created_at: datetime
    model: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    cache_read_tokens: Optional[int] = None  # Reported separately; excluded from costs.
    actual_cost: Optional[Decimal] = None
    baseline_cost: Optional[Decimal] = None
    savings: Optional[Decimal] = None
    quality_ok: Optional[bool] = None
    acceptance_rate: Optional[float] = None
    gate: Optional[str] = None
    findings_count: int
    # E20: the distinct CWE ids on this run's findings ("CWE-89", …), in finding
    # order — the runs table shows them without a drill-in. Empty on review runs.
    cwes: list[str] = []


class SavingsResponse(CamelModel):
    """The dashboard envelope (architecture §6): `{ kpis, series[], runs[] }` + the
    echoed `range`, plus the two model NAMES the chart legend/tooltip render so it's
    unambiguous on first view what "actual" (the recommended pick) vs "baseline" mean.
    """

    range: SavingsRange
    selected_model: Optional[str] = None   # the recommended model powering "actual"
    baseline_model: Optional[str] = None   # the expensive default "baseline" is costed against
    # E20: the project's task (catalog vocabulary) — names the task in the UI + chat.
    task_type: Optional[str] = None
    kpis: SavingsKpis
    series: list[SavingsSeriesPoint]
    runs: list[SavingsRunRow]


# --- findings drill-in (Fork 4: a thin read so the runs table drills in) ------

class FindingRow(CamelModel):
    """One finding on a run, with the current user's verdict (if any) — so the FE can
    show the S13 accept/reject state inline when drilling in from the runs table."""

    id: int
    severity: Optional[str] = None
    category: Optional[str] = None
    file: Optional[str] = None
    line: Optional[int] = None
    message: Optional[str] = None
    cwe: Optional[str] = None  # security task: "CWE-89: SQL Injection"; None on review
    verdict: Optional[str] = None  # the caller's accept/reject, or None if not yet rated


class RunFindingsResponse(CamelModel):
    """`GET /projects/{id}/runs/{run_id}/findings` → the run's findings (camelCase out)."""

    run_id: int
    findings: list[FindingRow]
