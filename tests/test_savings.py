"""S12 savings-engine tests (split input/output pricing).

Two layers:
- The PURE core (`compute_savings`) — known tokens × known split prices → exact
  `Decimal` savings. No DB, no LLM; the determinism guarantee.
- The wiring — catalog upsert populates `model.input/output_price_per_mtok` (with
  backfill from the legacy blended price); the `/ci-runs` ingest fills
  `actual_cost`/`baseline_cost`/`savings` and echoes them in `CiRunOut`; an unpriced
  model leaves the trio NULL without failing; and ingest makes ZERO LLM calls.
"""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.catalog import service as catalog_service
from app.catalog.seed import load_seed
from app.models import CiRun, LlmCall, LlmUsage, Model, Project, RecommendationOption
from app.savings.service import Pricing, compute_savings, price_for
from app.schemas.catalog import CatalogRowIn

# Reuse the S11 CI test helpers (register → recommend → project → mint token).
from tests.test_ci import _agent_result, _make_project, _mint_token, _register

# Demo split prices (per MTok): Claude Haiku 4.5 vs Claude Sonnet 4.x baseline.
HAIKU = Pricing(Decimal("1.0"), Decimal("5.0"))
SONNET = Pricing(Decimal("3.0"), Decimal("15.0"))


# --- pure core: known split numbers → exact Decimal -----------------------

def test_compute_savings_known_split_numbers():
    # tokens_in=1200, tokens_out=340.
    # actual (Haiku)    = 1200/1e6*1.0 + 340/1e6*5.0 = 0.001200 + 0.001700 = 0.002900
    # baseline (Sonnet) = 1200/1e6*3.0 + 340/1e6*15.0 = 0.003600 + 0.005100 = 0.008700
    # savings = 0.008700 - 0.002900 = 0.005800
    actual, baseline, savings = compute_savings(1200, 340, HAIKU, SONNET)
    assert actual == Decimal("0.002900")
    assert baseline == Decimal("0.008700")
    assert savings == Decimal("0.005800")
    assert savings == baseline - actual  # identity holds after quantization


def test_compute_savings_positive_when_selected_cheaper():
    _, _, savings = compute_savings(5000, 5000, HAIKU, SONNET)
    assert savings > 0


def test_compute_savings_negative_when_selected_dearer():
    # Selected Sonnet, baseline Haiku → negative "savings" (an honest overspend).
    _, _, savings = compute_savings(1200, 340, SONNET, HAIKU)
    assert savings == Decimal("-0.005800")


def test_compute_savings_is_deterministic():
    a = compute_savings(1000, 500, HAIKU, SONNET)
    b = compute_savings(1000, 500, HAIKU, SONNET)
    assert a == b


def test_compute_savings_results_are_decimal_not_float():
    actual, baseline, savings = compute_savings(7, 3, HAIKU, SONNET)
    assert all(isinstance(x, Decimal) for x in (actual, baseline, savings))


def test_compute_savings_zero_tokens_is_zero():
    actual, baseline, savings = compute_savings(0, 0, HAIKU, SONNET)
    assert actual == Decimal("0") and baseline == Decimal("0") and savings == Decimal("0")


def test_compute_savings_same_model_zero_savings():
    actual, baseline, savings = compute_savings(2000, 1000, SONNET, SONNET)
    assert actual == baseline
    assert savings == Decimal("0")


def test_compute_savings_quantizes_to_six_dp():
    # 1 input token at 0.5/MTok = 0.0000005 → rounds half-up to 0.000001 (6 dp).
    actual, _, _ = compute_savings(1, 0, Pricing(Decimal("0.5"), Decimal("0")), HAIKU)
    assert actual == Decimal("0.000001")


def test_compute_savings_input_and_output_priced_separately():
    # Same total tokens, different in/out split → different cost (proves it's NOT a
    # single blended price): output is dearer, so output-heavy costs more.
    in_heavy, _, _ = compute_savings(1000, 0, HAIKU, SONNET)   # all input @1.0
    out_heavy, _, _ = compute_savings(0, 1000, HAIKU, SONNET)  # all output @5.0
    assert in_heavy == Decimal("0.001000")
    assert out_heavy == Decimal("0.005000")
    assert out_heavy > in_heavy


@pytest.mark.parametrize("sel,base", [(None, SONNET), (HAIKU, None), (None, None)])
def test_compute_savings_unpriced_returns_none_trio(sel, base):
    assert compute_savings(1200, 340, sel, base) == (None, None, None)


# --- catalog upsert: split prices populated + backfilled -------------------

def test_seed_populates_split_prices(db_session):
    load_seed(db_session)
    sonnet = db_session.scalar(select(Model).where(Model.name == "Claude Sonnet 4.x"))
    haiku = db_session.scalar(select(Model).where(Model.name == "Claude Haiku 4.5"))
    assert (sonnet.input_price_per_mtok, sonnet.output_price_per_mtok) == (
        Decimal("3.0"),
        Decimal("15.0"),
    )
    assert (haiku.input_price_per_mtok, haiku.output_price_per_mtok) == (
        Decimal("1.0"),
        Decimal("5.0"),
    )


def test_upsert_backfills_split_from_blended_when_absent(db_session):
    # A row carrying ONLY the legacy blended cost_per_mtok → both split prices backfill.
    row = CatalogRowIn(
        model="LegacyModel", vendor="ACME", benchmark="SWE-bench Verified",
        metric="pass@1_percent", score=Decimal("40"), cost_per_mtok=Decimal("2.5"),
        task_type="agentic_coding",
    )
    catalog_service.upsert_catalog_row(db_session, row)
    m = db_session.scalar(select(Model).where(Model.name == "LegacyModel"))
    assert m.input_price_per_mtok == Decimal("2.5")
    assert m.output_price_per_mtok == Decimal("2.5")
    # And the resolved Pricing reproduces the old blended math (in == out == blended).
    assert price_for(db_session, m.id) == Pricing(Decimal("2.5"), Decimal("2.5"))


def test_price_for_resolves_and_handles_missing(db_session):
    load_seed(db_session)
    sonnet = db_session.scalar(select(Model).where(Model.name == "Claude Sonnet 4.x"))
    assert price_for(db_session, sonnet.id) == SONNET
    assert price_for(db_session, None) is None
    assert price_for(db_session, 999999) is None  # unknown id

    # A model missing one half of the split is treated as unpriced.
    sonnet.output_price_per_mtok = None
    db_session.commit()
    assert price_for(db_session, sonnet.id) is None


# --- wiring: ingest fills the trio + echoes it ----------------------------

def _resolved_pricing(db_session, pid: int) -> tuple[Pricing, Pricing]:
    """The (selected, baseline) split Pricing the project resolves to."""
    project = db_session.get(Project, pid)
    option = db_session.get(RecommendationOption, project.selected_option_id)
    return (
        price_for(db_session, option.model_id),
        price_for(db_session, project.baseline_model_id),
    )


def test_ingest_persists_savings_trio(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "sv_ingest@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)

    resp = client.post(
        f"/projects/{pid}/ci-runs",
        json=_agent_result("sv-1"),  # tokensIn=1200, tokensOut=340
        headers={"X-CI-Token": token},
    )
    assert resp.status_code == 201

    selected, baseline = _resolved_pricing(db_session, pid)
    exp_actual, exp_baseline, exp_savings = compute_savings(1200, 340, selected, baseline)

    run = db_session.scalar(select(CiRun).where(CiRun.project_id == pid))
    assert run.actual_cost == exp_actual
    assert run.baseline_cost == exp_baseline
    assert run.savings == exp_savings
    assert run.savings == run.baseline_cost - run.actual_cost
    # quality_ok stays S13's job — untouched here.
    assert run.quality_ok is None


def test_ingest_echoes_savings_in_response(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "sv_echo@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)

    out = client.post(
        f"/projects/{pid}/ci-runs",
        json=_agent_result("sv-echo-1"),
        headers={"X-CI-Token": token},
    ).json()

    selected, baseline = _resolved_pricing(db_session, pid)
    exp_actual, exp_baseline, exp_savings = compute_savings(1200, 340, selected, baseline)
    # camelCase out; Decimal serializes as a JSON number → compare via Decimal(str()).
    assert Decimal(str(out["actualCost"])) == exp_actual
    assert Decimal(str(out["baselineCost"])) == exp_baseline
    assert Decimal(str(out["savings"])) == exp_savings


def test_ingest_unpriced_model_leaves_trio_null(client, db_session):
    """If the selected model has no price, ingest still succeeds — trio stays NULL."""
    load_seed(db_session)
    headers, _ = _register(client, db_session, "sv_unpriced@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)

    # Wipe the selected model's prices to simulate an unpriced catalog model.
    project = db_session.get(Project, pid)
    option = db_session.get(RecommendationOption, project.selected_option_id)
    selected_model = db_session.get(Model, option.model_id)
    selected_model.input_price_per_mtok = None
    selected_model.output_price_per_mtok = None
    db_session.commit()

    resp = client.post(
        f"/projects/{pid}/ci-runs",
        json=_agent_result("sv-unpriced-1"),
        headers={"X-CI-Token": token},
    )
    assert resp.status_code == 201  # not failed
    run = db_session.scalar(select(CiRun).where(CiRun.project_id == pid))
    assert run.actual_cost is None
    assert run.baseline_cost is None
    assert run.savings is None


def test_ingest_duplicate_build_is_409_and_single_run(client, db_session):
    load_seed(db_session)
    headers, _ = _register(client, db_session, "sv_dup@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)
    hdr = {"X-CI-Token": token}

    assert client.post(f"/projects/{pid}/ci-runs", json=_agent_result("dup"), headers=hdr).status_code == 201
    assert client.post(f"/projects/{pid}/ci-runs", json=_agent_result("dup"), headers=hdr).status_code == 409
    n = db_session.scalar(select(func.count()).select_from(CiRun).where(CiRun.project_id == pid))
    assert n == 1


def test_ingest_makes_zero_llm_calls(client, db_session, monkeypatch):
    """Savings is deterministic arithmetic — the ingest path must NOT touch the LLM
    seam. Guard behaviourally (any build_llm_client use raises) AND by evidence (no
    llm_call / llm_usage accounting rows are written)."""
    import app.llm as llm_module

    def _boom(*args, **kwargs):  # pragma: no cover - only fires on regression
        raise AssertionError("S12 ingest must make NO LLM calls")

    monkeypatch.setattr(llm_module, "build_llm_client", _boom)

    load_seed(db_session)
    headers, _ = _register(client, db_session, "sv_nollm@example.com")
    pid = _make_project(client, headers)
    token = _mint_token(client, headers, pid)

    resp = client.post(
        f"/projects/{pid}/ci-runs",
        json=_agent_result("sv-nollm-1"),
        headers={"X-CI-Token": token},
    )
    assert resp.status_code == 201
    run = db_session.scalar(select(CiRun).where(CiRun.project_id == pid))
    assert run.savings is not None  # savings still computed, with zero LLM involvement
    assert db_session.scalar(select(func.count()).select_from(LlmCall)) == 0
    assert db_session.scalar(select(func.count()).select_from(LlmUsage)) == 0
