"""Demo dataset seeder (P30) — idempotent, deterministic, ZERO LLM tokens.

Brings an empty DB to a demo-ready state so a fresh bring-up shows a populated
dashboard with no manual steps:

  1. a demo user (get-or-create),
  2. a project built from a REAL deterministic recommendation (the same
     `recommend` + `create_project` services the API uses — no shortcut rows), and
  3. a deterministic spread of CI runs so the savings/quality panels show shape.

Invoked as the gated ArgoCD PostSync hook `python -m app.demo.seed` (after the
catalog seed), or locally. Everything here is deterministic and writes straight to
the configured DB via the ORM — there is NO LLM on this path (the recommender is a
pure scorer), so it spends zero tokens.

Idempotent / safe to re-run: the user and project are get-or-create; the runs are
reset (delete this project's `ci_run` rows — the FK cascade clears their findings +
feedback — then re-insert a fresh deterministic set). Re-firing on every sync just
restores the demo to a known-good state.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.auth.service import get_user_by_email, register_user
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
from app.projects.service import create_project
from app.recommend.service import recommend
from app.schemas.project import ProjectCreate
from app.schemas.recommend import RecommendationRequest

# The recommendation the demo project is built from: a cost-leaning CI-review pick
# (high budget sensitivity → cheapest acceptable model, historically Nova 2 Lite) vs
# the configured baseline (Sonnet). Deterministic — same catalog → same suggestion.
_DEMO_TASK_TYPES = ["ci_review"]
_DEMO_BUDGET_SENSITIVITY = "high"

_THRESHOLD = Decimal("0.8")  # mirrors QUALITY_THRESHOLD (S13)
_FINDINGS = [
    ("security", "high", "app/api/auth.py", 42, "Possible timing-unsafe token compare"),
    ("style", "low", "app/main.py", 17, "Unused import"),
    ("security", "medium", "app/db.py", 88, "Connection not closed on error path"),
    ("style", "low", "app/utils.py", 5, "Function exceeds 50 lines"),
    ("security", "high", "app/api/ci.py", 120, "User input concatenated into query"),
    ("style", "medium", "app/recommend/scoring.py", 31, "Magic number; extract constant"),
]


def make_runs(count: int = 30) -> list[tuple[int, int, int, int, int, int, int]]:
    """Deterministic CI-run generator — produces `count` runs across `count` days so
    the dashboard shows an arc: savings accumulate, with a few quality-risk runs
    (acceptance < 0.8) and a couple unrated runs (no feedback) so every KPI bucket +
    the quality dip is visible. accept/(accept+reject) >= QUALITY_THRESHOLD → the run
    banks. Same count → identical output (no randomness), so re-seeding is reproducible.
    Tuple shape: (build, days_ago, tokens_in, tokens_out, n_findings, n_accept, n_reject).
    """
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


def _cost(tokens_in: int, tokens_out: int, m: Model) -> Decimal:
    """Per-run cost from a model's split prices (USD). The 3:1 in:out ratio is already
    baked into the token counts — here we just apply each half's $/MTok."""
    cin = (Decimal(tokens_in) / Decimal(1_000_000)) * (m.input_price_per_mtok or Decimal(0))
    cout = (Decimal(tokens_out) / Decimal(1_000_000)) * (m.output_price_per_mtok or Decimal(0))
    return (cin + cout).quantize(Decimal("0.000001"))


def _ensure_user(db: Session, email: str, password: str) -> User:
    """Get-or-create the demo user. The password is hashed (argon2) via the same
    register_user service the API uses — never stored or logged in the clear."""
    user = get_user_by_email(db, email)
    if user is not None:
        return user
    return register_user(db, email, password)


def _ensure_project(db: Session, user: User, project_name: str) -> Project:
    """Get-or-create the demo project. On first run, generate a real recommendation
    (persists the options) and build the project from the suggested option + baseline
    via the same create_project service the API uses — so the demo exercises the real
    code path, not a hand-stitched row."""
    project = db.scalar(
        select(Project).where(
            Project.name == project_name, Project.user_id == user.id
        )
    )
    if project is not None:
        return project

    result = recommend(
        db,
        RecommendationRequest(
            task_types=_DEMO_TASK_TYPES,
            budget_sensitivity=_DEMO_BUDGET_SENSITIVITY,
        ),
        user,
    )
    out = create_project(
        db,
        ProjectCreate(
            name=project_name,
            selected_option_id=result.suggested.recommendation_option_id,
            baseline_model_id=result.baseline.model_id,
        ),
        user,
    )
    return db.get(Project, out.id)


def seed_runs(db: Session, project: Project, count: int) -> dict[str, object]:
    """Reset + re-insert this project's CI runs (+ findings + the owner's verdicts).
    Idempotent: drops the project's existing runs first (FK cascade clears findings +
    feedback). Costs use the project's OWN selected-vs-baseline split prices, so
    actual/baseline/savings stay internally consistent with the S12 savings engine."""
    selected_model_id = db.scalar(
        select(RecommendationOption.model_id).where(
            RecommendationOption.id == project.selected_option_id
        )
    )
    selected = db.get(Model, selected_model_id)
    baseline = db.get(Model, project.baseline_model_id)
    if selected is None or baseline is None:
        raise SystemExit("Project is missing a selected or baseline model.")

    db.execute(delete(CiRun).where(CiRun.project_id == project.id))
    db.flush()

    now = datetime.now(timezone.utc)
    banked = risk = unrated = 0
    for build, days_ago, tin, tout, n_find, n_acc, n_rej in make_runs(count):
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
    return {
        "runs": count,
        "banked": banked,
        "quality_risk": risk,
        "unrated": unrated,
        "selected": selected.name,
        "baseline": baseline.name,
    }


def seed_demo_data(
    db: Session,
    *,
    email: str,
    password: str,
    project_name: str = "demo-api",
    run_count: int = 30,
    force: bool = False,
) -> dict[str, object]:
    """Seed the full demo dataset once: user → project → CI runs. Returns a summary
    dict (run buckets + selected/baseline model names, or a skip marker).

    SKIP-IF-PRESENT — this is the demo half of a PostSync hook that re-fires on EVERY
    backend deploy (the release bumps this chart's migrate image tag, re-syncing the
    app). So once the demo project already has runs, return early WITHOUT touching the
    DB: the seed is a one-time bootstrap per fresh database, never a per-deploy reset
    that would redo work or wipe live demo activity. The user + project are
    get-or-create (also non-destructive). Pass force=True to re-seed deliberately
    (the local `scripts/seed_demo.py` path)."""
    user = _ensure_user(db, email, password)
    project = _ensure_project(db, user, project_name)

    existing = db.scalar(
        select(func.count()).select_from(CiRun).where(CiRun.project_id == project.id)
    )
    if existing and not force:
        return {
            "skipped": True,
            "runs": existing,
            "project": project.name,
            "user": user.email,
        }

    summary = seed_runs(db, project, run_count)
    summary["skipped"] = False
    summary["project"] = project.name
    summary["user"] = user.email
    return summary


def main() -> None:
    """CLI entrypoint (`python -m app.demo.seed`) — the demo half of the P30 auto-seed
    hook, gated by DEMO_SEED. Reads the demo user creds + run count from settings (the
    password arrives via ESO/Secrets Manager in-cluster, never Git). No-ops loudly if
    the flag is off so an accidental local run can't fabricate demo data."""
    from app.db import SessionLocal

    s = get_settings()
    if not s.demo_seed:
        print("DEMO_SEED is off — skipping demo dataset seed.")
        return
    if not s.demo_seed_email or not s.demo_seed_password:
        raise SystemExit(
            "DEMO_SEED is on but DEMO_SEED_EMAIL / DEMO_SEED_PASSWORD are unset."
        )

    db = SessionLocal()
    try:
        summary = seed_demo_data(
            db,
            email=s.demo_seed_email,
            password=s.demo_seed_password,
            project_name=s.demo_seed_project,
            run_count=s.demo_seed_run_count,
        )
        if summary.get("skipped"):
            print(
                f"demo dataset already present for {summary['project']!r} "
                f"({summary['runs']} runs) — skipping (no re-seed on redeploy)."
            )
        else:
            print(
                f"Seeded {summary['runs']} runs for {summary['project']!r} "
                f"(user {summary['user']}): {summary['banked']} banked · "
                f"{summary['quality_risk']} quality-risk · {summary['unrated']} unrated · "
                f"selected={summary['selected']} baseline={summary['baseline']}"
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
