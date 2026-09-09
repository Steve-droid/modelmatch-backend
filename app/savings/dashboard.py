"""Savings dashboard service (S14): owner-scoped reads behind the dashboard.

The wiring half of the aggregate (the pure assembly lives in `aggregate.py`):
- `project_savings` loads a project's `ci_run` rows (optionally windowed by `?range=`)
  + their finding counts and feedback verdicts, then hands them to `assemble`.
- `run_findings` is the runs-table drill-in: a project run's findings with the
  caller's accept/reject verdict (S13) inline.

Both are user-JWT + owner-scoped (this is a USER action, not the CI-token ingest path):
missing project/run → 404, someone else's → 403. DETERMINISTIC — pure reads, NO LLM,
zero tokens (same discipline as S12/S13).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import require_owner
from app.config import get_settings
from app.models import (
    CiFinding,
    CiRun,
    FindingFeedback,
    Model,
    Project,
    RecommendationOption,
    User,
)
from app.savings.aggregate import RunRecord, assemble
from app.schemas.savings import FindingRow, RunFindingsResponse, SavingsResponse

# `?range=` → window length. "all" (None) is the whole history.
_RANGE_DAYS: dict[str, Optional[int]] = {"all": None, "7d": 7, "30d": 30, "90d": 90}


def _require_owned_project(db: Session, project_id: int, current_user: User) -> Project:
    """Load a project the caller owns, or raise 404 (missing) / 403 (not yours)."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    require_owner(project.user_id, current_user)
    return project


def project_savings(
    db: Session, project_id: int, current_user: User, range_label: str = "all"
) -> SavingsResponse:
    """Assemble the dashboard DTO for an owned project's runs (optionally windowed)."""
    project = _require_owned_project(db, project_id, current_user)

    # The two model NAMES the chart legend/tooltip render (the recommended pick that
    # powers "actual", and the baseline it's costed against). One lookup each.
    option = (
        db.get(RecommendationOption, project.selected_option_id)
        if project.selected_option_id is not None
        else None
    )
    selected_model_obj = db.get(Model, option.model_id) if option else None
    baseline_model_obj = (
        db.get(Model, project.baseline_model_id)
        if project.baseline_model_id is not None
        else None
    )
    selected_model = selected_model_obj.name if selected_model_obj else None
    baseline_model = baseline_model_obj.name if baseline_model_obj else None

    now = datetime.now(timezone.utc)
    days = _RANGE_DAYS[range_label]
    cutoff = now - timedelta(days=days) if days is not None else None

    # The runs (+ the model name the table/series render), oldest → newest.
    stmt = (
        select(CiRun, Model.name)
        .outerjoin(Model, CiRun.model_id == Model.id)
        .where(CiRun.project_id == project_id)
        .order_by(CiRun.created_at, CiRun.id)
    )
    if cutoff is not None:
        stmt = stmt.where(CiRun.created_at >= cutoff)
    rows = db.execute(stmt).all()
    runs = [r[0] for r in rows]
    model_names = {r[0].id: r[1] for r in rows}
    run_ids = [run.id for run in runs]

    # Finding counts, CWE ids + feedback verdicts, grouped (no N+1) — empty when no runs.
    counts: dict[int, int] = {}
    cwes: dict[int, list[str]] = {}
    verdicts: dict[int, list[str]] = {}
    if run_ids:
        for run_id, n in db.execute(
            select(CiFinding.ci_run_id, func.count())
            .where(CiFinding.ci_run_id.in_(run_ids))
            .group_by(CiFinding.ci_run_id)
        ).all():
            counts[run_id] = n
        # E20: the distinct CWE ids per run ("CWE-89"), in finding order, so the runs
        # table can show them without a drill-in. The full "CWE-89: title" stays on
        # the finding (run_findings below).
        for run_id, cwe in db.execute(
            select(CiFinding.ci_run_id, CiFinding.cwe)
            .where(CiFinding.ci_run_id.in_(run_ids), CiFinding.cwe.is_not(None))
            .order_by(CiFinding.id)
        ).all():
            ident = cwe.split(":", 1)[0].strip()
            bucket = cwes.setdefault(run_id, [])
            if ident and ident not in bucket:
                bucket.append(ident)
        for run_id, verdict in db.execute(
            select(CiFinding.ci_run_id, FindingFeedback.verdict)
            .join(FindingFeedback, FindingFeedback.ci_finding_id == CiFinding.id)
            .where(CiFinding.ci_run_id.in_(run_ids))
        ).all():
            verdicts.setdefault(run_id, []).append(verdict)

    records = [
        RunRecord(
            id=run.id,
            created_at=run.created_at,
            jenkins_build_id=run.jenkins_build_id,
            model=model_names.get(run.id),
            tokens_in=run.tokens_in,
            tokens_out=run.tokens_out,
            cache_read_tokens=run.cache_read_tokens,
            actual_cost=run.actual_cost,
            baseline_cost=run.baseline_cost,
            savings=run.savings,
            quality_ok=run.quality_ok,
            gate=run.gate,
            findings_count=counts.get(run.id, 0),
            verdicts=tuple(verdicts.get(run.id, [])),
            cwes=tuple(cwes.get(run.id, [])),
        )
        for run in runs
    ]

    threshold = get_settings().quality_threshold
    return assemble(
        records,
        threshold,
        now,
        range_label,
        selected_model,
        baseline_model,
        task_type=project.task_type,
    )


def run_findings(
    db: Session, project_id: int, run_id: int, current_user: User
) -> RunFindingsResponse:
    """The runs-table drill-in: an owned project run's findings + the caller's verdicts."""
    _require_owned_project(db, project_id, current_user)

    run = db.get(CiRun, run_id)
    if run is None or run.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Run not found"
        )

    findings = list(
        db.scalars(
            select(CiFinding)
            .where(CiFinding.ci_run_id == run_id)
            .order_by(CiFinding.id)
        ).all()
    )
    finding_ids = [f.id for f in findings]

    # The caller's own verdict per finding (one per (finding, user) — S13).
    my_verdicts: dict[int, str] = {}
    if finding_ids:
        for fid, verdict in db.execute(
            select(FindingFeedback.ci_finding_id, FindingFeedback.verdict).where(
                FindingFeedback.ci_finding_id.in_(finding_ids),
                FindingFeedback.user_id == current_user.id,
            )
        ).all():
            my_verdicts[fid] = verdict

    return RunFindingsResponse(
        run_id=run_id,
        findings=[
            FindingRow(
                id=f.id,
                severity=f.severity,
                category=f.category,
                file=f.file,
                line=f.line,
                message=f.message,
                cwe=f.cwe,
                verdict=my_verdicts.get(f.id),
            )
            for f in findings
        ],
    )
