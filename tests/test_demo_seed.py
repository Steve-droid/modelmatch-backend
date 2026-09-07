"""P30 auto-seed tests: the catalog entrypoint and the idempotent demo dataset.

All deterministic — the recommender is a pure scorer and the run generator has no
randomness, so seeding spends ZERO LLM tokens (asserted via the empty token tallies).
"""

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.catalog.seed import load_seed
from app.demo.seed import seed_demo_data
from app.models import CiRun, JenkinsConnection, Project, User
from app.models.orm import LlmCall, LlmUsage
from app.projects.service import list_projects

_EMAIL = "demo-seed@example.com"
_PASSWORD = "demo-seed-pw-not-a-real-secret"


def test_catalog_seed_entrypoint_seeds_rows(migrated_engine, monkeypatch, capsys):
    """`python -m app.catalog.seed` → main() seeds the catalog. The entrypoint opens
    SessionLocal, so we point it at the throwaway test DB and assert the printed count."""
    import app.catalog.seed as catalog_seed

    test_session = sessionmaker(bind=migrated_engine, autoflush=False, autocommit=False)
    monkeypatch.setattr("app.db.SessionLocal", test_session)

    catalog_seed.main()

    import json

    from app.catalog.seed import SEED_PATH

    expected = len(json.loads(SEED_PATH.read_text())["benchmark_results"])
    assert f"catalog rows seeded: {expected}" in capsys.readouterr().out


def test_demo_seed_is_idempotent_and_spends_no_tokens(db_session):
    """First call seeds; a re-run (as a PostSync hook would) SKIPS without touching the
    DB — non-destructive, so a redeploy never wipes live demo activity. force=True
    re-seeds without duplicating. Never touches an LLM."""
    load_seed(db_session)  # catalog rows the recommender ranks over

    def counts():
        return (
            db_session.scalar(select(func.count()).select_from(User)),
            db_session.scalar(select(func.count()).select_from(Project)),
            db_session.scalar(select(func.count()).select_from(CiRun)),
        )

    first = seed_demo_data(
        db_session, email=_EMAIL, password=_PASSWORD, project_name="demo-api", run_count=30
    )
    # Deterministic buckets (matches the live demo): 24 banked · 3 risk · 3 unrated.
    assert first["skipped"] is False
    assert first["runs"] == 30
    assert first["banked"] == 24
    assert first["quality_risk"] == 3
    assert first["unrated"] == 30 - 24 - 3
    assert first["baseline"] == "Claude Sonnet 4.5"
    assert first["selected"] and first["selected"] != first["baseline"]
    assert counts() == (1, 1, 30)

    # Re-run (the per-deploy re-sync case): skips, no duplication, no wipe.
    second = seed_demo_data(
        db_session, email=_EMAIL, password=_PASSWORD, project_name="demo-api", run_count=30
    )
    assert second["skipped"] is True
    assert second["runs"] == 30
    assert counts() == (1, 1, 30)

    # Deliberate re-seed (force) resets without duplicating user/project/runs.
    third = seed_demo_data(
        db_session, email=_EMAIL, password=_PASSWORD, run_count=30, force=True
    )
    assert third["skipped"] is False
    assert counts() == (1, 1, 30)

    # Zero tokens: the deterministic path never records an LLM call or usage tally.
    assert db_session.scalar(select(func.count()).select_from(LlmCall)) == 0
    assert db_session.scalar(select(func.count()).select_from(LlmUsage)) == 0


def test_demo_seed_project_is_setup_complete(db_session):
    """B2: each seeded project owns a Jenkins connection AND a minted CI token, so the
    dashboard reads "setup complete" instead of badging the demo as half-configured.
    Re-seeding must not mint a second token (the connection upserts, the token is
    mint-once) — the demo never ingests over the API, so the plaintext is discarded.

    Asserted over BOTH demo projects, because the job name is derived per project: a
    constant here would label every project with the first one's job, and demo-sec
    would show "demo-api/main" on the dashboard and in the recording."""
    from app.demo.seed import seed_security_demo_data

    load_seed(db_session)
    seed_demo_data(
        db_session, email=_EMAIL, password=_PASSWORD, project_name="demo-api", run_count=5
    )
    seed_security_demo_data(
        db_session, email=_EMAIL, password=_PASSWORD, project_name="demo-sec", run_count=5
    )

    first_hashes = {}
    for name in ("demo-api", "demo-sec"):
        project = db_session.scalar(select(Project).where(Project.name == name))
        conn = db_session.scalar(
            select(JenkinsConnection).where(JenkinsConnection.project_id == project.id)
        )
        assert conn is not None
        assert conn.base_url == "https://jenkins.example.invalid"
        assert conn.job_name == f"{name}/main"
        assert conn.status == "configured"
        assert conn.ci_token_hash is not None  # minted; only the hash is stored
        first_hashes[name] = conn.ci_token_hash

    user = db_session.scalar(select(User).where(User.email == _EMAIL))
    assert [p.setup_complete for p in list_projects(db_session, user)] == [True, True]

    # a redeploy re-fires the hook: same connections, same tokens (no rotation)
    seed_demo_data(
        db_session, email=_EMAIL, password=_PASSWORD, project_name="demo-api", run_count=5
    )
    seed_security_demo_data(
        db_session, email=_EMAIL, password=_PASSWORD, project_name="demo-sec", run_count=5
    )
    for name in ("demo-api", "demo-sec"):
        project = db_session.scalar(select(Project).where(Project.name == name))
        conn = db_session.scalar(
            select(JenkinsConnection).where(JenkinsConnection.project_id == project.id)
        )
        assert conn.ci_token_hash == first_hashes[name]
        assert conn.job_name == f"{name}/main"
    assert (
        db_session.scalar(select(func.count()).select_from(JenkinsConnection)) == 2
    )


def test_demo_seed_main_skips_when_disabled(monkeypatch, capsys):
    """The gated entrypoint no-ops loudly (and never opens a DB) when DEMO_SEED is off,
    so an accidental local run can't fabricate demo data."""
    from app.config import get_settings

    monkeypatch.delenv("DEMO_SEED", raising=False)
    get_settings.cache_clear()
    try:
        import app.demo.seed as demo_seed

        demo_seed.main()
        assert "skipping" in capsys.readouterr().out.lower()
    finally:
        get_settings.cache_clear()


def test_security_demo_project_is_seeded_from_a_real_recommendation(db_session):
    """P38c: the second demo project, for the security-analysis task.

    Built through the same `recommend` + `create_project` services as the review demo
    — so the dashboard's security project is a real deterministic pick (DeepSeek V4
    Flash against the Claude Opus 5 baseline on RealVuln), not a hand-stitched row."""
    from app.demo.seed import seed_security_demo_data

    load_seed(db_session)

    summary = seed_security_demo_data(
        db_session, email=_EMAIL, password=_PASSWORD, project_name="demo-sec", run_count=20
    )

    assert summary["skipped"] is False
    assert summary["runs"] == 20
    assert summary["selected"] == "DeepSeek V4 Flash"
    assert summary["baseline"] == "Claude Opus 5"
    # Every dashboard bucket is populated. The exact split falls out of the SHARED
    # deterministic run generator (the same one the review demo uses) rather than
    # being tuned per project: over 20 runs its cycles land on 17 banked, 1
    # quality-risk and 2 unrated. Asserted exactly, because the value of a
    # deterministic seed is that these numbers cannot drift unnoticed.
    assert summary["banked"] == 17
    assert summary["quality_risk"] == 1
    assert summary["unrated"] == 2
    assert summary["banked"] + summary["quality_risk"] + summary["unrated"] == 20

    # re-running skips, exactly like the review demo (a PostSync hook re-fires on
    # every deploy and must never wipe live demo activity)
    again = seed_security_demo_data(
        db_session, email=_EMAIL, password=_PASSWORD, project_name="demo-sec", run_count=20
    )
    assert again["skipped"] is True


def test_the_two_demo_projects_coexist_under_one_user(db_session):
    """Both demo projects belong to the same demo user, so one login shows both
    tasks — and seeding one does not disturb the other's runs."""
    from app.demo.seed import seed_demo_data, seed_security_demo_data

    load_seed(db_session)
    seed_demo_data(db_session, email=_EMAIL, password=_PASSWORD, project_name="demo-api", run_count=30)
    seed_security_demo_data(db_session, email=_EMAIL, password=_PASSWORD, project_name="demo-sec", run_count=20)

    assert db_session.scalar(select(func.count()).select_from(User)) == 1
    projects = db_session.scalars(select(Project).order_by(Project.name)).all()
    assert [p.name for p in projects] == ["demo-api", "demo-sec"]
    assert db_session.scalar(select(func.count()).select_from(CiRun)) == 50

    # zero tokens on both paths — the recommender is a pure scorer
    assert db_session.scalar(select(func.count()).select_from(LlmCall)) == 0


def test_security_runs_use_agentic_scan_token_volumes(db_session):
    """A security scan reads a whole repository; a review reads one diff. If both
    demos used the same token counts the dashboard's cost-per-run panel would be
    telling a false story about what each task costs to operate."""
    from app.demo.seed import seed_demo_data, seed_security_demo_data

    load_seed(db_session)
    seed_demo_data(db_session, email=_EMAIL, password=_PASSWORD, project_name="demo-api", run_count=30)
    seed_security_demo_data(db_session, email=_EMAIL, password=_PASSWORD, project_name="demo-sec", run_count=20)

    by_name = {p.name: p for p in db_session.scalars(select(Project)).all()}
    review_runs = db_session.scalars(
        select(CiRun).where(CiRun.project_id == by_name["demo-api"].id)
    ).all()
    security_runs = db_session.scalars(
        select(CiRun).where(CiRun.project_id == by_name["demo-sec"].id)
    ).all()

    # roughly two orders of magnitude apart, matching RealVuln's published per-repo
    # averages for an agentic scan (~134k input tokens)
    assert max(r.tokens_in for r in review_runs) < 2_000
    assert all(100_000 < r.tokens_in < 200_000 for r in security_runs)
    assert all(r.task == "security_analysis" for r in security_runs)

    # every security run still saves money against the Opus 5 baseline
    assert all(r.savings > 0 for r in security_runs)


def test_security_findings_carry_cwe_identifiers(db_session):
    """Security findings speak CWE — the vocabulary the agent's Semgrep-shaped output
    reports in, and what the findings table must be able to display."""
    from app.demo.seed import seed_security_demo_data
    from app.models import CiFinding

    load_seed(db_session)
    seed_security_demo_data(db_session, email=_EMAIL, password=_PASSWORD, run_count=20)

    findings = db_session.scalars(select(CiFinding)).all()
    assert findings
    assert all(f.category == "security" for f in findings)
    assert all("CWE-" in f.message for f in findings)
    assert any(f.severity == "critical" for f in findings)
