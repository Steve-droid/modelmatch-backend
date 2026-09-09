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
from app.ci.service import ci_setup
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
from app.projects import jenkins_service
from app.projects.service import create_project
from app.recommend.service import recommend
from app.schemas.jenkins import JenkinsConnectionUpdate
from app.schemas.project import ProjectCreate
from app.schemas.recommend import RecommendationRequest
from app.tasks import CI_REVIEW, SECURITY_ANALYSIS

# The recommendation the demo project is built from: a cost-leaning CI-review pick
# (high budget sensitivity → cheapest acceptable model, historically Nova 2 Lite) vs
# the configured baseline (Sonnet). Deterministic — same catalog → same suggestion.
_DEMO_TASK_TYPES = [CI_REVIEW]
_DEMO_BUDGET_SENSITIVITY = "high"

# E20: the review demo's per-project preferences — what the agent appends to its
# prompt (served by GET /agent-config), so the demo shows the feature without a
# hand edit. Applied when the project has none yet; never overwrites a user's edit.
_DEMO_REVIEW_PREFERENCES = (
    "Flag any use of eval() or exec() as high. Treat request handlers that skip "
    "input validation as high. Do not report import ordering or docstring style."
)

# The demo projects' Jenkins connection. A placeholder host on the reserved .invalid
# TLD — the demo never calls Jenkins (runs are seeded straight into the DB), it just
# needs the connection + a minted CI token so the project reads "setup complete"
# (app/projects/service.py: setup_complete = conn is not None and ci_token_hash is
# not None) instead of badging "Setup incomplete" on the dashboard.
#
# The job name is DERIVED from the project name rather than being a constant: every
# demo project gets its own `<project>/main` label. A literal here would badge every
# project with the first one's job — demo-sec would read "demo-api/main" on the
# dashboard and in the recording.
_DEMO_JENKINS_BASE_URL = "https://jenkins.example.invalid"
_DEMO_JENKINS_JOB_SUFFIX = "main"

# P38c: a SECOND demo project for the security-analysis task, so the dashboard shows
# the product's two tasks side by side — a cheap reviewer on every PR, and an agentic
# vulnerability scan whose findings gate the build. Cost-leaning on RealVuln picks
# DeepSeek V4 Flash against the Claude Opus 5 baseline.
_DEMO_SECURITY_TASK_TYPES = [SECURITY_ANALYSIS]
_DEMO_SECURITY_BUDGET_SENSITIVITY = "high"

# Token volumes differ by an order of magnitude between the two tasks, and the demo
# would look wrong if they did not. A review reads ONE diff and answers in a few
# hundred tokens. A security scan is an agentic loop that reads a whole repository:
# RealVuln's own Gemini 3.5 Flash run averaged 133,767 input and 4,170 output tokens
# per repository, which is the scale used here. That figure describes the WORKLOAD, not
# the model — the same repository is the same code to read whichever model scans it — so
# it stays the anchor now that the pick is DeepSeek V4 Flash.
_REVIEW_TOKENS = (1_100, 650, 290, 200)  # (in_base, in_spread, out_base, out_spread)
_SECURITY_TOKENS = (120_000, 30_000, 3_600, 1_200)

# Security findings carry a CWE — the vocabulary a security scanner reports in, and
# what the P38d agent emits from its Semgrep-shaped output. Tuple shape:
# (category, severity, file, line, message, cwe) — the cwe is ALSO at the head of the
# message so a pre-E20 row (no column) still reads right in the drill-in.
_SECURITY_FINDINGS = [
    ("security", "critical", "app/api/search.py", 64, "CWE-89: SQL injection — request parameter concatenated into a query", "CWE-89: SQL injection"),
    ("security", "high", "app/auth/session.py", 28, "CWE-798: hard-coded credential used as a signing key", "CWE-798: hard-coded credential"),
    ("security", "high", "app/api/files.py", 96, "CWE-22: path traversal — user input joined to a filesystem path", "CWE-22: path traversal"),
    ("security", "medium", "app/templates/profile.html", 12, "CWE-79: reflected cross-site scripting in a rendered field", "CWE-79: cross-site scripting"),
    ("security", "medium", "app/net/client.py", 41, "CWE-918: server-side request forgery — URL taken from the request", "CWE-918: server-side request forgery"),
    ("security", "low", "app/crypto/hash.py", 19, "CWE-327: weak hash (MD5) used for a security decision", "CWE-327: broken or risky crypto"),
]

_THRESHOLD = Decimal("0.8")  # mirrors QUALITY_THRESHOLD (S13)
_FINDINGS = [  # review findings carry no CWE (the review task is not CWE-tagged)
    ("security", "high", "app/api/auth.py", 42, "Possible timing-unsafe token compare", None),
    ("style", "low", "app/main.py", 17, "Unused import", None),
    ("security", "medium", "app/db.py", 88, "Connection not closed on error path", None),
    ("style", "low", "app/utils.py", 5, "Function exceeds 50 lines", None),
    ("security", "high", "app/api/ci.py", 120, "User input concatenated into query", None),
    ("style", "medium", "app/recommend/scoring.py", 31, "Magic number; extract constant", None),
]


def make_runs(
    count: int = 30,
    tokens: tuple[int, int, int, int] = _REVIEW_TOKENS,
) -> list[tuple[int, int, int, int, int, int, int]]:
    """Deterministic CI-run generator — produces `count` runs across `count` days so
    the dashboard shows an arc: savings accumulate, with a few quality-risk runs
    (acceptance < 0.8) and a couple unrated runs (no feedback) so every KPI bucket +
    the quality dip is visible. accept/(accept+reject) >= QUALITY_THRESHOLD → the run
    banks. Same count → identical output (no randomness), so re-seeding is reproducible.
    Tuple shape: (build, days_ago, tokens_in, tokens_out, n_findings, n_accept, n_reject).
    """
    in_base, in_spread, out_base, out_spread = tokens
    runs: list[tuple[int, int, int, int, int, int, int]] = []
    for i in range(count):
        build = 201 + i
        days_ago = count - i  # oldest first; most recent run = 1 day ago
        tin = in_base + (i * 37) % in_spread  # deterministic spread, no randomness
        tout = out_base + (i * 13) % out_spread
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


def _ensure_jenkins_setup(db: Session, project: Project, user: User) -> None:
    """Give the demo project a Jenkins connection + a minted CI token, through the
    same services the API uses (`connect_jenkins`, then `ci_setup`). Without this the
    dashboard badges the project "Setup incomplete", which reads as a broken demo.

    Idempotent: `connect_jenkins` upserts the single per-project connection, and
    `ci_setup` mints only when `ci_token_hash` is still NULL. The minted plaintext
    token is DISCARDED here on purpose — the seed writes CI runs directly to the DB
    and never ingests over the API, so nothing needs it (and nothing logs it)."""
    jenkins_service.connect_jenkins(
        db,
        project.id,
        JenkinsConnectionUpdate(
            base_url=_DEMO_JENKINS_BASE_URL,
            job_name=f"{project.name}/{_DEMO_JENKINS_JOB_SUFFIX}",
        ),
        user,
    )
    ci_setup(db, project.id, user)  # mints the token on first run; the value is dropped


def _ensure_project(
    db: Session,
    user: User,
    project_name: str,
    *,
    task_types: list[str] | None = None,
    budget_sensitivity: str | None = None,
    review_preferences: str | None = None,
) -> Project:
    """Get-or-create the demo project. On first run, generate a real recommendation
    (persists the options) and build the project from the suggested option + baseline
    via the same create_project service the API uses — so the demo exercises the real
    code path, not a hand-stitched row. Either way the project ends up with a Jenkins
    connection + CI token (setup complete), the task it runs (E20) and, for the review
    demo, preferences if it has none yet."""
    task_type = (task_types or _DEMO_TASK_TYPES)[0]
    project = db.scalar(
        select(Project).where(
            Project.name == project_name, Project.user_id == user.id
        )
    )
    if project is None:
        result = recommend(
            db,
            RecommendationRequest(
                task_types=task_types or _DEMO_TASK_TYPES,
                budget_sensitivity=budget_sensitivity or _DEMO_BUDGET_SENSITIVITY,
            ),
            user,
        )
        out = create_project(
            db,
            ProjectCreate(
                name=project_name,
                selected_option_id=result.suggested.recommendation_option_id,
                baseline_model_id=result.baseline.model_id,
                task_type=task_type,
                review_preferences=review_preferences,
            ),
            user,
        )
        project = db.get(Project, out.id)
    else:
        # An existing (pre-E20) demo project: state its task, and give the review demo
        # its preferences ONLY if none are set (a user's edit is never overwritten).
        if project.task_type != task_type:
            project.task_type = task_type
        if review_preferences and project.review_preferences is None:
            project.review_preferences = review_preferences
        db.commit()

    _ensure_jenkins_setup(db, project, user)
    return project


def seed_runs(
    db: Session,
    project: Project,
    count: int,
    *,
    tokens: tuple[int, int, int, int] = _REVIEW_TOKENS,
    findings: list[tuple[str, str, str, int, str, str | None]] | None = None,
    task: str = CI_REVIEW,
) -> dict[str, object]:
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
    finding_set = findings or _FINDINGS
    for build, days_ago, tin, tout, n_find, n_acc, n_rej in make_runs(count, tokens):
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
            task=task,
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
            cat, sev, fpath, line, msg, cwe = finding_set[i % len(finding_set)]
            finding = CiFinding(
                ci_run_id=run.id, severity=sev, category=cat,
                file=fpath, line=line, message=msg, cwe=cwe,
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
    project = _ensure_project(
        db, user, project_name, review_preferences=_DEMO_REVIEW_PREFERENCES
    )

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


def seed_security_demo_data(
    db: Session,
    *,
    email: str,
    password: str,
    project_name: str = "demo-sec",
    run_count: int = 20,
    force: bool = False,
) -> dict[str, object]:
    """Seed the SECURITY demo project (P38c) — the second of the product's two tasks.

    Same shape and the same skip-if-present contract as `seed_demo_data`, but built
    from a `security_analysis` recommendation (DeepSeek V4 Flash against the Claude
    Opus 5 baseline, per RealVuln), with agentic-scan token volumes and CWE findings.
    Kept as its own function rather than a branch inside `seed_demo_data` so the
    original demo project's behaviour is provably untouched.
    """
    user = _ensure_user(db, email, password)
    project = _ensure_project(
        db,
        user,
        project_name,
        task_types=_DEMO_SECURITY_TASK_TYPES,
        budget_sensitivity=_DEMO_SECURITY_BUDGET_SENSITIVITY,
    )

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

    summary = seed_runs(
        db,
        project,
        run_count,
        tokens=_SECURITY_TOKENS,
        findings=_SECURITY_FINDINGS,
        task=SECURITY_ANALYSIS,
    )
    summary["skipped"] = False
    summary["project"] = project.name
    summary["user"] = user.email
    return summary


def _print_summary(summary: dict[str, object]) -> None:
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
        _print_summary(
            seed_demo_data(
                db,
                email=s.demo_seed_email,
                password=s.demo_seed_password,
                project_name=s.demo_seed_project,
                run_count=s.demo_seed_run_count,
            )
        )
        # The security project (P38c) — same demo user, second task. Gated separately
        # so an environment can seed only the review demo if it wants to.
        if s.demo_seed_security_project:
            _print_summary(
                seed_security_demo_data(
                    db,
                    email=s.demo_seed_email,
                    password=s.demo_seed_password,
                    project_name=s.demo_seed_security_project,
                    run_count=s.demo_seed_security_run_count,
                )
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
