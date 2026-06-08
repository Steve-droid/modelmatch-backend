#!/usr/bin/env python
"""Seed a demo project with a spread of CI runs so the dashboard panels show shape
over time (savings accumulating, quality trend dipping, cost-per-run coloured by gate).

This is DEMO TOOLING, not product code or test fixtures — it writes straight to the
configured database via the ORM. It is idempotent per project: it deletes the target
project's existing `ci_run` rows first (the S15d FK cascade removes their findings +
feedback), then inserts a fresh, deterministic set.

Costs are computed from the project's OWN selected vs baseline model split prices (the
same inputs the S12 savings engine uses), so actual/baseline/savings are internally
consistent. Per-run acceptance comes from real `ci_finding` + `finding_feedback` rows
(owned by the project's user), so the quality trend + KPIs populate honestly.

Usage (from modelmatch-backend/, with the app env set — DATABASE_URL etc.):
    uv run python scripts/seed_demo.py [project_name]   # default: demo-api
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import (
    CiFinding,
    CiRun,
    FindingFeedback,
    Model,
    Project,
    RecommendationOption,
)

# (build, days_ago, tokens_in, tokens_out, n_findings, n_accept, n_reject)
# A 3-week arc: savings accumulate; mostly-passing quality with two quality-risk runs
# (acceptance < 0.8) and two unrated runs (no feedback) so every KPI bucket + the
# quality dip is visible. accept/(accept+reject) >= QUALITY_THRESHOLD (0.8) → banks.
_RUNS = [
    (201, 20, 1400, 380, 4, 4, 0),  # 100% banked
    (202, 19, 1350, 360, 3, 3, 0),  # 100% banked
    (203, 18, 1500, 410, 5, 4, 1),  # 80%  banked (at threshold)
    (204, 17, 1280, 340, 4, 0, 0),  # unrated (no feedback)
    (205, 16, 1600, 430, 6, 3, 3),  # 50%  quality risk
    (206, 14, 1330, 350, 4, 4, 0),  # 100% banked
    (207, 12, 1450, 390, 5, 4, 1),  # 80%  banked
    (208, 10, 1220, 320, 3, 3, 0),  # 100% banked
    (209, 8, 1700, 450, 6, 2, 3),   # 40%  quality risk
    (210, 6, 1300, 340, 4, 4, 0),   # 100% banked
    (211, 4, 1180, 300, 3, 0, 0),   # unrated
    (212, 3, 1500, 400, 5, 5, 0),   # 100% banked
    (213, 2, 1100, 280, 3, 3, 0),   # 100% banked
    (214, 1, 1050, 270, 2, 2, 0),   # 100% banked
]

_THRESHOLD = Decimal("0.8")  # mirrors QUALITY_THRESHOLD (S13)
_FINDINGS = [
    ("security", "high", "app/api/auth.py", 42, "Possible timing-unsafe token compare"),
    ("style", "low", "app/main.py", 17, "Unused import"),
    ("security", "medium", "app/db.py", 88, "Connection not closed on error path"),
    ("style", "low", "app/utils.py", 5, "Function exceeds 50 lines"),
    ("security", "high", "app/api/ci.py", 120, "User input concatenated into query"),
    ("style", "medium", "app/recommend/scoring.py", 31, "Magic number; extract constant"),
]


def _cost(tokens_in: int, tokens_out: int, m: Model) -> Decimal:
    """Per-run cost from a model's split prices (USD), 3:1 in:out already baked into the
    token counts — here we just apply each half's $/MTok."""
    cin = (Decimal(tokens_in) / Decimal(1_000_000)) * (m.input_price_per_mtok or Decimal(0))
    cout = (Decimal(tokens_out) / Decimal(1_000_000)) * (m.output_price_per_mtok or Decimal(0))
    return (cin + cout).quantize(Decimal("0.000001"))


def seed(project_name: str = "demo-api") -> None:
    db = SessionLocal()
    try:
        project = db.scalar(select(Project).where(Project.name == project_name))
        if project is None:
            raise SystemExit(f"No project named {project_name!r} — create it first.")

        # The selected model (what the agent runs) vs the baseline (costed, not run).
        selected_model_id = db.scalar(
            select(RecommendationOption.model_id).where(
                RecommendationOption.id == project.selected_option_id
            )
        )
        selected = db.get(Model, selected_model_id)
        baseline = db.get(Model, project.baseline_model_id)
        if selected is None or baseline is None:
            raise SystemExit("Project is missing a selected or baseline model.")

        # Idempotent: drop this project's runs (cascade clears findings + feedback).
        db.execute(delete(CiRun).where(CiRun.project_id == project.id))
        db.flush()

        now = datetime.now(timezone.utc)
        banked = risk = unrated = 0
        for build, days_ago, tin, tout, n_find, n_acc, n_rej in _RUNS:
            rated = n_acc + n_rej
            if rated == 0:
                quality_ok = None
                unrated += 1
            else:
                acc_rate = Decimal(n_acc) / Decimal(rated)
                quality_ok = acc_rate >= _THRESHOLD
                banked += 1 if quality_ok else 0
                risk += 0 if quality_ok else 1

            actual = _cost(tin, tout, selected)
            base = _cost(tin, tout, baseline)
            run = CiRun(
                project_id=project.id,
                jenkins_build_id=str(build),
                model_id=selected.id,
                task="code_review",
                tokens_in=tin,
                tokens_out=tout,
                actual_cost=actual,
                baseline_cost=base,
                savings=(base - actual),
                quality_ok=quality_ok,
                gate="pass" if quality_ok is not False else "fail",
                created_at=now - timedelta(days=days_ago),
            )
            db.add(run)
            db.flush()

            # findings + the owner's verdicts (first n_acc accept, next n_rej reject)
            for i in range(n_find):
                cat, sev, fpath, line, msg = _FINDINGS[i % len(_FINDINGS)]
                finding = CiFinding(
                    ci_run_id=run.id, severity=sev, category=cat,
                    file=fpath, line=line, message=msg,
                )
                db.add(finding)
                db.flush()
                if i < rated:
                    db.add(FindingFeedback(
                        ci_finding_id=finding.id,
                        verdict="accept" if i < n_acc else "reject",
                        user_id=project.user_id,
                    ))

        db.commit()
        print(
            f"Seeded {len(_RUNS)} runs for {project_name!r} (id {project.id}): "
            f"{banked} banked · {risk} quality-risk · {unrated} unrated · "
            f"selected={selected.name} baseline={baseline.name}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    seed(sys.argv[1] if len(sys.argv) > 1 else "demo-api")
