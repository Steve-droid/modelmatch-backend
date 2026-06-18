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
# Deterministic run generator — produces `count` CI runs across `count` days so the
# dashboard shows an arc: savings accumulate, with a few quality-risk runs (acceptance
# < 0.8) and a couple unrated runs (no feedback) so every KPI bucket + the quality dip
# is visible. accept/(accept+reject) >= QUALITY_THRESHOLD (0.8) → the run banks.
# Tuple shape: (build, days_ago, tokens_in, tokens_out, n_findings, n_accept, n_reject).
# Same count → identical output (no randomness), so re-seeding is reproducible.
def _make_runs(count: int = 30) -> list[tuple[int, int, int, int, int, int, int]]:
    runs: list[tuple[int, int, int, int, int, int, int]] = []
    for i in range(count):
        build = 201 + i
        days_ago = count - i  # oldest first; most recent run = 1 day ago
        tin = 1100 + (i * 37) % 650  # 1100–1749, deterministic spread
        tout = 290 + (i * 13) % 200  # 290–489
        n_find = 2 + (i % 5)  # 2–6 findings
        if i % 9 == 4:  # ~1 in 9 → unrated (no feedback at all)
            n_acc, n_rej = 0, 0
        elif i % 7 == 6:  # ~1 in 7 → quality risk (acceptance < 0.8)
            n_rej = max(2, (n_find + 1) // 2)
            n_acc = n_find - n_rej
        else:  # banked: every finding accepted (100%)
            n_acc, n_rej = n_find, 0
        runs.append((build, days_ago, tin, tout, n_find, n_acc, n_rej))
    return runs

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


def seed(project_name: str = "demo-api", count: int = 30) -> None:
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
        runs = _make_runs(count)
        banked = risk = unrated = 0
        for build, days_ago, tin, tout, n_find, n_acc, n_rej in runs:
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
            f"Seeded {len(runs)} runs for {project_name!r} (id {project.id}): "
            f"{banked} banked · {risk} quality-risk · {unrated} unrated · "
            f"selected={selected.name} baseline={baseline.name}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "demo-api"
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    seed(name, count)
