"""S13 quality-gate tests.

Three layers (mirrors S12's structure):
- The PURE core (`acceptance_rate`, `recompute_quality`, `honest_cumulative_savings`)
  — known numbers → exact result. No DB, no LLM; the determinism guarantee.
- The feedback endpoint — accept/reject persists (upsert, not duplicate), owner-scoped
  (403), unknown finding (404), bad verdict (422); the run's `quality_ok` flips as
  feedback crosses the threshold.
- The honest cumulative — sub-threshold runs are EXCLUDED from the headline yet
  surfaced as "quality risk"; and the feedback path makes ZERO LLM calls.
"""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.catalog.seed import load_seed
from app.models import CiFinding, CiRun, FindingFeedback, LlmCall, LlmUsage
from app.quality.service import acceptance_rate, recompute_quality
from app.savings.service import honest_cumulative_savings

# Reuse the S11 CI helpers (register → recommend → project → mint token → ingest).
from tests.test_ci import _agent_result, _make_project, _mint_token, _register

THRESHOLD = 0.8  # the default QUALITY_THRESHOLD the tests assume


# --- pure core: acceptance_rate -------------------------------------------

def test_acceptance_rate_four_accept_one_reject_is_point_eight():
    assert acceptance_rate(["accept", "accept", "accept", "accept", "reject"]) == 0.8


def test_acceptance_rate_three_accept_two_reject_is_point_six():
    assert acceptance_rate(["accept", "accept", "accept", "reject", "reject"]) == 0.6


def test_acceptance_rate_empty_is_none():
    assert acceptance_rate([]) is None


def test_acceptance_rate_all_accept_is_one():
    assert acceptance_rate(["accept", "accept"]) == 1.0


def test_acceptance_rate_all_reject_is_zero():
    assert acceptance_rate(["reject", "reject"]) == 0.0


# --- pure core: recompute_quality (gate) ----------------------------------

def test_recompute_quality_point_eight_passes_at_threshold():
    # 4 accept / 1 reject = 0.8 ≥ 0.8 → pass.
    verdicts = ["accept", "accept", "accept", "accept", "reject"]
    assert recompute_quality(verdicts, THRESHOLD) is True


def test_recompute_quality_point_six_fails():
    # 3 accept / 2 reject = 0.6 < 0.8 → fail.
    verdicts = ["accept", "accept", "accept", "reject", "reject"]
    assert recompute_quality(verdicts, THRESHOLD) is False


def test_recompute_quality_empty_is_none():
    assert recompute_quality([], THRESHOLD) is None


def test_recompute_quality_is_deterministic():
    v = ["accept", "reject", "accept"]
    assert recompute_quality(v, THRESHOLD) == recompute_quality(v, THRESHOLD)


def test_recompute_quality_respects_threshold_value():
    # The same verdicts pass under a lenient threshold and fail under a strict one.
    verdicts = ["accept", "accept", "accept", "reject"]  # 0.75
    assert recompute_quality(verdicts, 0.7) is True
    assert recompute_quality(verdicts, 0.8) is False


# --- pure core: honest cumulative savings ---------------------------------

def test_honest_cumulative_counts_only_quality_passing():
    runs = [
        (Decimal("0.005"), True),    # banked
        (Decimal("0.003"), True),    # banked
        (Decimal("0.009"), False),   # quality risk — excluded from headline
        (Decimal("0.002"), None),    # unrated — neither
    ]
    agg = honest_cumulative_savings(runs)
    assert agg.cumulative == Decimal("0.008000")  # 0.005 + 0.003 only
    assert agg.banked_runs == 2
    assert agg.quality_risk == Decimal("0.009000")  # surfaced, not dropped
    assert agg.quality_risk_runs == 1
    assert agg.unrated_runs == 1


def test_honest_cumulative_null_savings_counts_as_zero_money():
    # An unpriced (NULL savings) but quality-passing run banks no money but is counted.
    agg = honest_cumulative_savings([(None, True)])
    assert agg.cumulative == Decimal("0")
    assert agg.banked_runs == 1


def test_honest_cumulative_empty_is_all_zero():
    agg = honest_cumulative_savings([])
    assert agg.cumulative == Decimal("0")
    assert agg.quality_risk == Decimal("0")
    assert (agg.banked_runs, agg.quality_risk_runs, agg.unrated_runs) == (0, 0, 0)


# --- wiring helpers --------------------------------------------------------

def _run_with_findings(client, db_session, headers, pid, token, build_id):
    """Ingest a run (2 findings from _agent_result) → (run_id, [finding_id, ...])."""
    resp = client.post(
        f"/projects/{pid}/ci-runs",
        json=_agent_result(build_id),
        headers={"X-CI-Token": token},
    )
    assert resp.status_code == 201
    run_id = resp.json()["id"]
    finding_ids = list(
        db_session.scalars(
            select(CiFinding.id).where(CiFinding.ci_run_id == run_id).order_by(CiFinding.id)
        ).all()
    )
    return run_id, finding_ids


# --- feedback endpoint -----------------------------------------------------

def test_feedback_accept_persists_and_gates_run(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "q_accept@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)
    run_id, fids = _run_with_findings(client, db_session, headers, pid, token, "q-acc")

    # Accept BOTH findings → rate 1.0 ≥ 0.8 → quality_ok True.
    for fid in fids:
        out = client.post(f"/findings/{fid}/feedback", json={"verdict": "accept"}, headers=headers)
        assert out.status_code == 200

    body = out.json()
    assert body["verdict"] == "accept"
    assert body["acceptanceRate"] == 1.0
    assert body["qualityOk"] is True

    db_session.expire_all()
    run = db_session.get(CiRun, run_id)
    assert run.quality_ok is True
    assert db_session.scalar(
        select(func.count()).select_from(FindingFeedback)
    ) == len(fids)


def test_feedback_below_threshold_marks_run_not_ok(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "q_fail@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)
    run_id, fids = _run_with_findings(client, db_session, headers, pid, token, "q-fail")

    # 1 accept / 1 reject = 0.5 < 0.8 → quality_ok False.
    client.post(f"/findings/{fids[0]}/feedback", json={"verdict": "accept"}, headers=headers)
    out = client.post(f"/findings/{fids[1]}/feedback", json={"verdict": "reject"}, headers=headers).json()
    assert out["acceptanceRate"] == 0.5
    assert out["qualityOk"] is False

    db_session.expire_all()
    assert db_session.get(CiRun, run_id).quality_ok is False


def test_feedback_flips_quality_ok_when_verdict_changes(client, db_session):
    """A re-feedback (upsert) that crosses the threshold flips the stored gate."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "q_flip@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)
    run_id, fids = _run_with_findings(client, db_session, headers, pid, token, "q-flip")

    # Start sub-threshold: accept one, reject the other → 0.5 → False.
    client.post(f"/findings/{fids[0]}/feedback", json={"verdict": "accept"}, headers=headers)
    client.post(f"/findings/{fids[1]}/feedback", json={"verdict": "reject"}, headers=headers)
    db_session.expire_all()
    assert db_session.get(CiRun, run_id).quality_ok is False

    # Change the reject → accept (upsert, not a new row) → 1.0 → flips to True.
    out = client.post(f"/findings/{fids[1]}/feedback", json={"verdict": "accept"}, headers=headers).json()
    assert out["qualityOk"] is True

    db_session.expire_all()
    assert db_session.get(CiRun, run_id).quality_ok is True
    # Upsert: still exactly one feedback row per finding (no duplicates).
    assert db_session.scalar(select(func.count()).select_from(FindingFeedback)) == 2


def test_repeated_same_verdict_is_idempotent_single_row(client, db_session):
    """Re-POSTing the same verdict upserts — one row per (finding, user), no growth."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "q_idem@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)
    _, fids = _run_with_findings(client, db_session, headers, pid, token, "q-idem")

    for _ in range(3):
        out = client.post(f"/findings/{fids[0]}/feedback", json={"verdict": "accept"}, headers=headers)
        assert out.status_code == 200

    db_session.expire_all()
    rows = db_session.scalar(
        select(func.count()).select_from(FindingFeedback).where(
            FindingFeedback.ci_finding_id == fids[0]
        )
    )
    assert rows == 1


def test_db_constraint_rejects_duplicate_feedback(client, db_session):
    """The invariant is enforced in the DB (not just app code): a second raw insert
    for the same (finding, user) violates uq_finding_feedback_finding_user. This is
    what protects against a TOCTOU race between parallel replica POSTs."""
    from sqlalchemy.exc import IntegrityError

    load_seed(db_session)
    headers, uid = _register(client, db_session, "q_dbuq@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)
    _, fids = _run_with_findings(client, db_session, headers, pid, token, "q-dbuq")

    db_session.add(FindingFeedback(ci_finding_id=fids[0], verdict="accept", user_id=uid))
    db_session.commit()
    db_session.add(FindingFeedback(ci_finding_id=fids[0], verdict="reject", user_id=uid))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_feedback_locks_ci_run_row_for_update(client, db_session, migrated_engine):
    """Concurrency guard: submit_feedback locks the CiRun row FOR UPDATE before
    recomputing quality_ok, so parallel feedback on the same run is serialized (no
    partial-recompute / stale-gate race). Proven deterministically by holding the
    row lock on a SECOND connection and asserting the service path blocks on it
    (lock_timeout fires → OperationalError), which can only happen if it issues
    SELECT ... FOR UPDATE on that same row."""
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as OrmSession

    from app.models import User
    from app.quality.service import submit_feedback
    from app.schemas.feedback import FeedbackIn

    load_seed(db_session)
    headers, uid = _register(client, db_session, "q_lock@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)
    run_id, fids = _run_with_findings(client, db_session, headers, pid, token, "q-lock")

    locker = OrmSession(migrated_engine)
    try:
        # Hold a FOR UPDATE lock on the run from a separate connection (txn left open).
        held = locker.scalar(select(CiRun).where(CiRun.id == run_id).with_for_update())
        assert held is not None

        # The service must contend for the SAME row lock → it blocks; cap the wait so
        # the assertion is deterministic. If submit_feedback did NOT lock the row, the
        # upsert+recompute would race through and this would raise nothing.
        db_session.execute(text("SET lock_timeout = '750ms'"))
        user = db_session.get(User, uid)
        with pytest.raises(OperationalError):
            submit_feedback(db_session, fids[0], FeedbackIn(verdict="accept"), user)
        db_session.rollback()
    finally:
        locker.rollback()
        locker.close()


def test_feedback_unknown_finding_is_404(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "q_404@example.com")
    resp = client.post("/findings/999999/feedback", json={"verdict": "accept"}, headers=headers)
    assert resp.status_code == 404


def test_feedback_bad_verdict_is_422(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "q_422@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)
    _, fids = _run_with_findings(client, db_session, headers, pid, token, "q-422")
    resp = client.post(f"/findings/{fids[0]}/feedback", json={"verdict": "maybe"}, headers=headers)
    assert resp.status_code == 422


def test_feedback_requires_auth_401(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "q_401@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)
    _, fids = _run_with_findings(client, db_session, headers, pid, token, "q-401")
    resp = client.post(f"/findings/{fids[0]}/feedback", json={"verdict": "accept"})  # no JWT
    assert resp.status_code == 401


def test_feedback_other_user_is_403(client, db_session):
    """Owner-scoping via finding → run → project → user: a stranger gets 403."""
    load_seed(db_session)
    owner_headers, _ = _register(client, db_session, "q_owner@example.com")
    pid = _make_project(client, owner_headers)
    token = _mint_token(client, owner_headers, pid)
    _, fids = _run_with_findings(client, db_session, owner_headers, pid, token, "q-403")

    stranger_headers, _ = _register(client, db_session, "q_stranger@example.com")
    resp = client.post(
        f"/findings/{fids[0]}/feedback", json={"verdict": "accept"}, headers=stranger_headers
    )
    assert resp.status_code == 403


# --- honest cumulative end-to-end (the backlog's "done means") -------------

def test_cumulative_savings_excludes_sub_threshold_run(client, db_session):
    """End-to-end: two priced runs, one gated pass + one gated fail. The honest
    cumulative banks ONLY the passing run; the failing run is excluded yet surfaced."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "q_cum@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)

    run_pass, pass_fids = _run_with_findings(client, db_session, headers, pid, token, "cum-pass")
    run_fail, fail_fids = _run_with_findings(client, db_session, headers, pid, token, "cum-fail")

    # Run A: accept both → pass. Run B: accept one, reject one → fail.
    for fid in pass_fids:
        client.post(f"/findings/{fid}/feedback", json={"verdict": "accept"}, headers=headers)
    client.post(f"/findings/{fail_fids[0]}/feedback", json={"verdict": "accept"}, headers=headers)
    client.post(f"/findings/{fail_fids[1]}/feedback", json={"verdict": "reject"}, headers=headers)

    db_session.expire_all()
    rows = db_session.execute(
        select(CiRun.savings, CiRun.quality_ok).where(CiRun.project_id == pid)
    ).all()
    agg = honest_cumulative_savings([(s, q) for s, q in rows])

    s_pass = db_session.get(CiRun, run_pass).savings
    s_fail = db_session.get(CiRun, run_fail).savings
    assert s_pass is not None and s_fail is not None  # both priced (same tokens → equal)

    # Headline banks only the passing run; the failing one is excluded but surfaced.
    assert agg.cumulative == s_pass
    assert agg.banked_runs == 1
    assert agg.quality_risk == s_fail
    assert agg.quality_risk_runs == 1
    assert agg.cumulative != s_pass + s_fail  # the naive (S12) total would double-count


def test_feedback_makes_zero_llm_calls(client, db_session, monkeypatch):
    """The quality gate is deterministic arithmetic over human verdicts — the feedback
    path must NOT touch the LLM seam. Guard behaviourally (build_llm_client raises) AND
    by evidence (no llm_call / llm_usage rows). Copied from S12's ingest guard."""
    import app.llm as llm_module

    def _boom(*args, **kwargs):  # pragma: no cover - only fires on regression
        raise AssertionError("S13 feedback must make NO LLM calls")

    monkeypatch.setattr(llm_module, "build_llm_client", _boom)

    load_seed(db_session)
    headers, _ = _register(client, db_session, "q_nollm@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)
    run_id, fids = _run_with_findings(client, db_session, headers, pid, token, "q-nollm")

    out = client.post(f"/findings/{fids[0]}/feedback", json={"verdict": "accept"}, headers=headers)
    assert out.status_code == 200
    assert db_session.scalar(select(func.count()).select_from(LlmCall)) == 0
    assert db_session.scalar(select(func.count()).select_from(LlmUsage)) == 0
