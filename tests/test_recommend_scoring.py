"""S6 pure-scoring tests: determinism + the comparability rule.

No DB, no LLM — score_and_rank is a pure function of (rows, weight). These are the
product's transparency guarantee: same inputs → same output, and a Pass@1 row is
never normalized against an accuracy row.
"""

from decimal import Decimal

from app.recommend.scoring import RankedItem, ScoreInput, score_and_rank

# A single comparability group (SWE-bench Verified / pass@1_percent), like the seed.
SWE = "SWE-bench Verified"
P1 = "pass@1_percent"


def _swe(key: int, model: str, score: float, cost: float) -> ScoreInput:
    return ScoreInput(
        key=key, model=model, benchmark=SWE, metric=P1,
        score=Decimal(str(score)), cost=Decimal(str(cost)),
    )


def _rows() -> list[ScoreInput]:
    return [
        _swe(1, "DeepSWE-32B", 42.2, 0.27),
        _swe(2, "Opus", 67.0, 15.0),
        _swe(3, "Sonnet", 60.0, 3.0),
        _swe(4, "Haiku", 45.0, 0.8),
    ]


def test_same_inputs_same_output_regardless_of_order():
    rows = _rows()
    a = score_and_rank(rows, Decimal("0.60"))
    b = score_and_rank(list(reversed(rows)), Decimal("0.60"))
    # identical ranking order and identical rank_score values
    assert [r.key for r in a.ranked] == [r.key for r in b.ranked]
    assert [r.rank_score for r in a.ranked] == [r.rank_score for r in b.ranked]


def test_slider_shifts_the_ranking():
    rows = _rows()
    # high budget-sensitivity → w_q=0.40 (cost-leaning): cheap, decent DeepSWE wins.
    cost_leaning = score_and_rank(rows, Decimal("0.40"))
    assert cost_leaning.ranked[0].model == "DeepSWE-32B"

    # low budget-sensitivity → w_q=0.85 (quality-leaning): the cheap model drops and
    # the best *value-at-high-quality* model (Sonnet) rises. Note even quality-leaning
    # doesn't pick Opus — 5× the price for +7 points isn't worth it under this formula.
    quality_leaning = score_and_rank(rows, Decimal("0.85"))
    assert quality_leaning.ranked[0].model == "Sonnet"

    # the slider genuinely moves the cheap model: rank 1 cost-leaning → last quality-leaning
    rank = lambda res, m: next(r.rank for r in res.ranked if r.model == m)  # noqa: E731
    assert rank(cost_leaning, "DeepSWE-32B") < rank(quality_leaning, "DeepSWE-32B")


def test_normalization_is_per_group_max():
    rows = _rows()
    res = score_and_rank(rows, Decimal("0.60"))
    by_model = {r.model: r for r in res.ranked}
    # quality normalized against the in-group max score (Opus 67.0)
    assert by_model["Opus"].quality == Decimal("1.000000")
    assert by_model["Sonnet"].quality == (Decimal("60.0") / Decimal("67.0")).quantize(Decimal("0.000001"))
    # cost normalized against the in-group max cost (Opus 15.0)
    assert by_model["Opus"].cost_norm == Decimal("1.000000")


def test_comparability_rule_picks_dominant_group_and_isolates_it():
    # Two groups: SWE-bench/pass@1 (3 rows, dominant) and MRCR/accuracy (2 rows).
    rows = [
        _swe(1, "DeepSWE-32B", 42.2, 0.27),
        _swe(2, "Sonnet", 60.0, 3.0),
        _swe(3, "Opus", 67.0, 15.0),
        ScoreInput(4, "Opus", "MRCR (long-context)", "accuracy_percent", Decimal("78.0"), Decimal("15.0")),
        ScoreInput(5, "Nova Lite", "MRCR (long-context)", "accuracy_percent", Decimal("70.0"), Decimal("0.06")),
    ]
    res = score_and_rank(rows, Decimal("0.60"))
    assert res.group == (SWE, P1)
    # only the dominant group's rows are ranked — the accuracy rows are excluded
    assert {r.key for r in res.ranked} == {1, 2, 3}
    # and the in-group max score is 67.0 (Opus), NOT 78.0 from the accuracy group
    top = next(r for r in res.ranked if r.model == "Opus")
    assert top.quality == Decimal("1.000000")


def test_ranks_are_dense_and_one_based():
    res = score_and_rank(_rows(), Decimal("0.60"))
    assert [r.rank for r in res.ranked] == [1, 2, 3, 4]


def test_empty_input_yields_empty_result():
    res = score_and_rank([], Decimal("0.60"))
    assert res.group is None
    assert res.ranked == []


def test_returns_rankeditems():
    res = score_and_rank(_rows(), Decimal("0.60"))
    assert all(isinstance(r, RankedItem) for r in res.ranked)
