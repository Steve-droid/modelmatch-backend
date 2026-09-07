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

    assert "catalog rows seeded: 9" in capsys.readouterr().out


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
    """B2: the seeded project owns a Jenkins connection AND a minted CI token, so the
    dashboard reads "setup complete" instead of badging the demo as half-configured.
    Re-seeding must not mint a second token (the connection upserts, the token is
    mint-once) — the demo never ingests over the API, so the plaintext is discarded."""
    load_seed(db_session)
    seed_demo_data(
        db_session, email=_EMAIL, password=_PASSWORD, project_name="demo-api", run_count=5
    )

    project = db_session.scalar(select(Project).where(Project.name == "demo-api"))
    conn = db_session.scalar(
        select(JenkinsConnection).where(JenkinsConnection.project_id == project.id)
    )
    assert conn is not None
    assert conn.base_url == "https://jenkins.example.invalid"
    assert conn.job_name == "demo-api/main"
    assert conn.status == "configured"
    assert conn.ci_token_hash is not None  # minted; only the hash is stored
    first_hash = conn.ci_token_hash

    user = db_session.scalar(select(User).where(User.email == _EMAIL))
    assert list_projects(db_session, user)[0].setup_complete is True

    # a redeploy re-fires the hook: same connection, same token (no rotation)
    seed_demo_data(
        db_session, email=_EMAIL, password=_PASSWORD, project_name="demo-api", run_count=5
    )
    db_session.refresh(conn)
    assert conn.ci_token_hash == first_hash
    assert (
        db_session.scalar(select(func.count()).select_from(JenkinsConnection)) == 1
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
