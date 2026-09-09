"""S14 savings-dashboard tests: the pure aggregate + the owner-scoped reads.

Three layers (mirrors S12/S13):
- The PURE core (`assemble`) — synthetic records → exact KPIs/series/runs. No DB, no
  LLM; the determinism + honesty guarantee (banked-only headline, quality risk
  surfaced, unrated counted, monthly projection).
- `GET /projects/{id}/savings` — the envelope shape, owner-scoping (401/403/404),
  `?range=` validation (422), a quality-risk run EXCLUDED from the headline yet present
  in `runs[]`, and ZERO LLM calls.
- `GET /projects/{id}/runs/{run_id}/findings` — the drill-in: owner-scoped, the
  caller's verdict inline.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select

from app.catalog.seed import load_seed
from app.models import LlmCall, LlmUsage
from app.savings.aggregate import RunRecord, assemble
from app.schemas.savings import SavingsResponse

# Reuse the CI + quality helpers (register → project → token → ingest + feedback).
from tests.test_ci import _agent_result, _make_project, _mint_token, _register
from tests.test_quality import _run_with_findings

THRESHOLD = 0.8
_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


def _rec(rid, *, savings, quality_ok, actual="0", baseline="0", verdicts=(), days_ago=0):
    return RunRecord(
        id=rid,
        created_at=_NOW - timedelta(days=days_ago),
        jenkins_build_id=f"b-{rid}",
        model="claude-haiku-4-5",
        tokens_in=1200,
        tokens_out=340,
        actual_cost=Decimal(actual),
        baseline_cost=Decimal(baseline),
        savings=Decimal(savings) if savings is not None else None,
        quality_ok=quality_ok,
        gate="pass",
        findings_count=len(verdicts),
        verdicts=tuple(verdicts),
    )


# --- pure core: assemble ---------------------------------------------------

def test_assemble_headline_banks_only_quality_passing():
    records = [
        _rec(1, savings="0.005", quality_ok=True, actual="0.002", baseline="0.007"),
        _rec(2, savings="0.003", quality_ok=True, actual="0.001", baseline="0.004"),
        _rec(3, savings="0.009", quality_ok=False, actual="0.002", baseline="0.011"),
        _rec(4, savings="0.002", quality_ok=None, actual="0.001", baseline="0.003"),
    ]
    dto = assemble(records, THRESHOLD, _NOW)
    k = dto.kpis
    assert k.cumulative_saved == Decimal("0.008000")     # banked only (0.005 + 0.003)
    assert k.banked_runs == 2
    assert k.quality_risk == Decimal("0.009000")         # surfaced, not dropped
    assert k.quality_risk_runs == 1
    assert k.unrated_runs == 1
    assert k.runs_count == 4
    # spend_this_period = Σ actual over ALL runs in window.
    assert k.spend_this_period == Decimal("0.006000")
    # baseline_total = Σ baseline over BANKED runs (the % denominator).
    assert k.baseline_total == Decimal("0.011000")       # 0.007 + 0.004


def test_assemble_saved_pct_is_headline_over_banked_baseline():
    records = [_rec(1, savings="2", quality_ok=True, actual="8", baseline="10")]
    dto = assemble(records, THRESHOLD, _NOW)
    assert dto.kpis.saved_pct == 20.0  # 2 / 10 * 100


def test_assemble_saved_pct_none_when_no_banked_baseline():
    # Only an unrated run → nothing banked → no denominator.
    dto = assemble([_rec(1, savings=None, quality_ok=None)], THRESHOLD, _NOW)
    assert dto.kpis.saved_pct is None


def test_assemble_quality_status_from_overall_acceptance():
    banking = assemble(
        [_rec(1, savings="1", quality_ok=True, verdicts=["accept", "accept"])],
        THRESHOLD, _NOW,
    )
    assert banking.kpis.quality_status == "banking"
    assert banking.kpis.acceptance_rate == 1.0

    risk = assemble(
        [_rec(1, savings="1", quality_ok=False, verdicts=["accept", "reject"])],
        THRESHOLD, _NOW,
    )
    assert risk.kpis.quality_status == "quality_risk"
    assert risk.kpis.acceptance_rate == 0.5

    unrated = assemble([_rec(1, savings="1", quality_ok=None)], THRESHOLD, _NOW)
    assert unrated.kpis.quality_status == "unrated"
    assert unrated.kpis.acceptance_rate is None


def test_assemble_projects_monthly_over_observed_span():
    # One banked run 30 days ago: spend 1.0/30d → 1.0/mo; savings 3.0/30d → 3.0/mo.
    rec = _rec(1, savings="3.0", quality_ok=True, actual="1.0", baseline="4.0", days_ago=30)
    dto = assemble([rec], THRESHOLD, _NOW)
    assert dto.kpis.projected_monthly_spend == Decimal("1.000000")
    assert dto.kpis.projected_monthly_savings == Decimal("3.000000")


def test_assemble_no_projection_under_one_day():
    # A run from "now" → < 1 day of history → honest None, not a fabricated figure.
    dto = assemble([_rec(1, savings="3", quality_ok=True, actual="1", baseline="4")], THRESHOLD, _NOW)
    assert dto.kpis.projected_monthly_spend is None
    assert dto.kpis.projected_monthly_savings is None


def test_assemble_projection_scales_for_partial_month():
    # Golden for the scaling factor itself (the 30-day case is exactly ×1 and hides it):
    # one banked run 15 days ago → span 15d → amount / 15 * 30 = amount × 2.
    rec = _rec(1, savings="3.0", quality_ok=True, actual="1.0", baseline="4.0", days_ago=15)
    dto = assemble([rec], THRESHOLD, _NOW)
    assert dto.kpis.projected_monthly_spend == Decimal("2.000000")    # 1.0 / 15 * 30
    assert dto.kpis.projected_monthly_savings == Decimal("6.000000")  # 3.0 / 15 * 30


def test_assemble_empty_is_all_zero():
    dto = assemble([], THRESHOLD, _NOW)
    assert dto.kpis.cumulative_saved == Decimal("0")
    assert dto.kpis.runs_count == 0
    assert dto.series == [] and dto.runs == []


def test_assemble_series_and_runs_ordered_oldest_first():
    records = [
        _rec(2, savings="1", quality_ok=True, days_ago=1),
        _rec(1, savings="1", quality_ok=True, days_ago=5),
    ]
    dto = assemble(records, THRESHOLD, _NOW)
    assert [r.id for r in dto.runs] == [1, 2]           # 5d ago before 1d ago
    assert dto.series[0].date < dto.series[1].date
    assert dto.runs[0].findings_count == 0


# --- GET /projects/{id}/savings: envelope + auth ---------------------------

def test_savings_envelope_validates_against_schema(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "sv_env@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)
    _run_with_findings(client, db_session, headers, pid, token, "env-1")

    resp = client.get(f"/projects/{pid}/savings", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "range",
        "taskType",
        "selectedModel",
        "baselineModel",
        "kpis",
        "series",
        "runs",
    }
    assert body["range"] == "all"
    # camelCase out; the response round-trips through the schema.
    SavingsResponse.model_validate(body)
    assert "cumulativeSaved" in body["kpis"]
    assert "qualityStatus" in body["kpis"]
    # the legend names (recommended pick + baseline) are present + distinct
    assert body["selectedModel"] and body["baselineModel"]
    assert body["selectedModel"] != body["baselineModel"]
    assert len(body["runs"]) == 1
    assert body["runs"][0]["findingsCount"] == 2
    # each series point names its run so a tooltip can label it
    assert body["series"][0]["jenkinsBuildId"] == "env-1"


def test_savings_requires_auth_401(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "sv_401@example.com")
    pid = _make_project(client, headers)
    resp = client.get(f"/projects/{pid}/savings")  # no JWT
    assert resp.status_code == 401


def test_savings_other_user_is_403(client, db_session):
    load_seed(db_session)
    owner_headers, _ = _register(client, db_session, "sv_owner@example.com")
    pid = _make_project(client, owner_headers)
    stranger_headers, _ = _register(client, db_session, "sv_stranger@example.com")
    resp = client.get(f"/projects/{pid}/savings", headers=stranger_headers)
    assert resp.status_code == 403


def test_savings_unknown_project_is_404(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "sv_404@example.com")
    resp = client.get("/projects/999999/savings", headers=headers)
    assert resp.status_code == 404


def test_savings_bad_range_is_422(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "sv_422@example.com")
    pid = _make_project(client, headers)
    resp = client.get(f"/projects/{pid}/savings?range=yesterday", headers=headers)
    assert resp.status_code == 422


def test_savings_empty_project_is_zeroed(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "sv_empty@example.com")
    pid = _make_project(client, headers)
    body = client.get(f"/projects/{pid}/savings", headers=headers).json()
    assert body["kpis"]["runsCount"] == 0
    assert Decimal(str(body["kpis"]["cumulativeSaved"])) == 0
    assert body["runs"] == [] and body["series"] == []


# --- the honesty story, end-to-end -----------------------------------------

def test_savings_excludes_quality_risk_run_from_headline_but_surfaces_it(client, db_session):
    """Two priced runs (same tokens → equal savings): one gated pass, one gated fail.
    The headline banks ONLY the passing run; the failing one is excluded yet present in
    runs[] flagged qualityOk=false (architecture §8 — never silently dropped)."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "sv_honest@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)

    _, pass_fids = _run_with_findings(client, db_session, headers, pid, token, "h-pass")
    _, fail_fids = _run_with_findings(client, db_session, headers, pid, token, "h-fail")
    for fid in pass_fids:  # accept both → pass
        client.post(f"/findings/{fid}/feedback", json={"verdict": "accept"}, headers=headers)
    client.post(f"/findings/{fail_fids[0]}/feedback", json={"verdict": "accept"}, headers=headers)
    client.post(f"/findings/{fail_fids[1]}/feedback", json={"verdict": "reject"}, headers=headers)

    k = client.get(f"/projects/{pid}/savings", headers=headers).json()["kpis"]
    assert k["bankedRuns"] == 1
    assert k["qualityRiskRuns"] == 1
    cumulative = Decimal(str(k["cumulativeSaved"]))
    quality_risk = Decimal(str(k["qualityRisk"]))
    # Equal-token runs → equal savings → the risk run would have ~doubled the headline.
    assert cumulative > 0 and quality_risk > 0
    assert cumulative == quality_risk  # same tokens → same per-run savings

    runs = client.get(f"/projects/{pid}/savings", headers=headers).json()["runs"]
    quality_flags = sorted(r["qualityOk"] for r in runs)
    assert quality_flags == [False, True]  # both present; the risk run is flagged, not dropped


def test_savings_makes_zero_llm_calls(client, db_session, monkeypatch):
    """The dashboard read is deterministic aggregation — it must NOT touch the LLM
    seam. Guard behaviourally (build_llm_client raises) AND by evidence (no
    llm_call / llm_usage rows). Copied from S12/S13's guard."""
    import app.llm as llm_module

    def _boom(*args, **kwargs):  # pragma: no cover - only fires on regression
        raise AssertionError("S14 savings read must make NO LLM calls")

    monkeypatch.setattr(llm_module, "build_llm_client", _boom)

    load_seed(db_session)
    headers, _ = _register(client, db_session, "sv_nollm@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)
    _run_with_findings(client, db_session, headers, pid, token, "nollm-1")

    resp = client.get(f"/projects/{pid}/savings", headers=headers)
    assert resp.status_code == 200
    assert db_session.scalar(select(func.count()).select_from(LlmCall)) == 0
    assert db_session.scalar(select(func.count()).select_from(LlmUsage)) == 0


# --- GET /projects/{id}/runs/{run_id}/findings: the drill-in ---------------

def test_run_findings_returns_findings_with_caller_verdict(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "fd_ok@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)
    run_id, fids = _run_with_findings(client, db_session, headers, pid, token, "fd-1")
    client.post(f"/findings/{fids[0]}/feedback", json={"verdict": "accept"}, headers=headers)

    body = client.get(f"/projects/{pid}/runs/{run_id}/findings", headers=headers).json()
    assert body["runId"] == run_id
    assert len(body["findings"]) == 2
    by_id = {f["id"]: f for f in body["findings"]}
    assert by_id[fids[0]]["verdict"] == "accept"  # the caller's verdict inline
    assert by_id[fids[1]]["verdict"] is None       # not yet rated


def test_run_findings_unknown_run_is_404(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "fd_404@example.com")
    pid = _make_project(client, headers)
    resp = client.get(f"/projects/{pid}/runs/999999/findings", headers=headers)
    assert resp.status_code == 404


def test_run_findings_other_project_run_is_404(client, db_session):
    """A run that exists but belongs to a different project → 404 (not cross-readable)."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "fd_cross@example.com")
    pid_a = _make_project(client, headers)
    token_a = _mint_token(client, headers, pid_a)
    run_a, _ = _run_with_findings(client, db_session, headers, pid_a, token_a, "cross-a")

    pid_b = _make_project(client, headers)  # same owner, different project
    resp = client.get(f"/projects/{pid_b}/runs/{run_a}/findings", headers=headers)
    assert resp.status_code == 404


def test_run_findings_other_user_is_403(client, db_session):
    load_seed(db_session)
    owner_headers, _ = _register(client, db_session, "fd_owner@example.com")
    pid = _make_project(client, owner_headers)
    token = _mint_token(client, owner_headers, pid)
    run_id, _ = _run_with_findings(client, db_session, owner_headers, pid, token, "fd-403")

    stranger_headers, _ = _register(client, db_session, "fd_stranger@example.com")
    resp = client.get(f"/projects/{pid}/runs/{run_id}/findings", headers=stranger_headers)
    assert resp.status_code == 403
