"""Deterministic scoring — the heart of the recommender (NO LLM, NO embeddings).

A pure function of (catalog rows, weight): same inputs → same output. The LLM
never ranks; this transparent formula does. From architecture §3.1:

    within a comparability group (same benchmark + same metric):
      quality = score / max_score_in_group        # 0..1
      cost    = cost_per_mtok / max_cost_in_group  # 0..1
      rank_score = w_q · quality + w_c · (1 − cost) # w_q + w_c = 1

**Comparability rule (hard, P38c):** rows are only ever normalized against others
in the same (benchmark, metric) group — never Pass@1 against accuracy. Cross-group
rank_scores aren't comparable (different normalization bases), so they are never
merged.

Until P38c, rows spanning several groups were resolved by a VOTE: the group with
the most rows was ranked and the rest were silently discarded. That made the
recommendation depend on how many rows each benchmark happened to contribute — add
two rows to the losing benchmark and the winner changes for reasons that have
nothing to do with quality or cost. Since P38c each task type owns exactly one
(benchmark, metric) pair, enforced at write time by
`catalog.service.upsert_catalog_row`. So more than one group surviving the filter
means the data or the query is wrong, and we RAISE instead of silently choosing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# rank_score / normalized values are quantized to 6 dp so persisted Numeric(14,6)
# values and in-memory comparisons agree exactly (determinism, not float drift).
_Q = Decimal("0.000001")
_ZERO = Decimal(0)
_ONE = Decimal(1)


class MultipleComparabilityGroups(Exception):
    """Raised when the rows handed to `score_and_rank` span >1 (benchmark, metric).

    Not a user error and not recoverable by guessing: scores from different
    benchmarks have different normalization bases, so any pick made across them
    would be arbitrary. The caller surfaces it rather than ranking a subset.
    """

    def __init__(self, groups: list[tuple[str, str]]) -> None:
        self.groups = groups
        rendered = ", ".join(f"({b} · {m})" for b, m in groups)
        super().__init__(
            "Catalog rows span more than one comparability group: "
            f"{rendered}. Exactly one (benchmark, metric) pair per task type is "
            "required — scores from different benchmarks are not comparable."
        )


@dataclass(frozen=True)
class ScoreInput:
    """One catalog row, reduced to exactly what scoring needs (DB-free)."""

    key: int  # benchmark_result id — opaque identity, carried through to evidence
    model: str
    benchmark: str
    metric: str
    score: Decimal
    cost: Decimal  # cost_per_mtok


@dataclass(frozen=True)
class RankedItem:
    key: int
    model: str
    benchmark: str
    metric: str
    score: Decimal
    cost: Decimal
    quality: Decimal  # score / max_score_in_group
    cost_norm: Decimal  # cost / max_cost_in_group
    rank_score: Decimal
    rank: int  # 1-based


@dataclass(frozen=True)
class RankResult:
    group: tuple[str, str] | None  # the (benchmark, metric) group ranked, or None
    ranked: list[RankedItem]


def _group_key(item: ScoreInput) -> tuple[str, str]:
    return (item.benchmark, item.metric)


def score_and_rank(items: list[ScoreInput], w_q: Decimal) -> RankResult:
    """Rank rows within their dominant comparability group. Pure + order-independent.

    w_q is the quality weight (0..1); w_c = 1 − w_q is the cost weight.
    """
    if not items:
        return RankResult(group=None, ranked=[])

    w_q = Decimal(w_q)
    w_c = _ONE - w_q

    # Exactly ONE comparability group may survive the filter (P38c). Two groups is a
    # data/query bug, not something to resolve by counting rows — see the module
    # docstring. Sorted so the error message is deterministic.
    groups = sorted({_group_key(it) for it in items})
    if len(groups) > 1:
        raise MultipleComparabilityGroups(groups)
    group = groups[0]

    rows = list(items)
    max_score = max((r.score for r in rows), default=_ZERO)
    max_cost = max((r.cost for r in rows), default=_ZERO)

    scored: list[RankedItem] = []
    for r in rows:
        quality = (r.score / max_score) if max_score > _ZERO else _ZERO
        cost_norm = (r.cost / max_cost) if max_cost > _ZERO else _ZERO
        rank_score = (w_q * quality + w_c * (_ONE - cost_norm)).quantize(_Q)
        scored.append(
            RankedItem(
                key=r.key,
                model=r.model,
                benchmark=r.benchmark,
                metric=r.metric,
                score=r.score,
                cost=r.cost,
                quality=quality.quantize(_Q),
                cost_norm=cost_norm.quantize(_Q),
                rank_score=rank_score,
                rank=0,  # assigned after sorting
            )
        )

    # Deterministic order: rank_score desc, then cheaper first, then model name,
    # then key — a total order, so equal scores never reorder run-to-run.
    scored.sort(key=lambda x: (-x.rank_score, x.cost, x.model, x.key))
    ranked = [
        RankedItem(
            key=s.key,
            model=s.model,
            benchmark=s.benchmark,
            metric=s.metric,
            score=s.score,
            cost=s.cost,
            quality=s.quality,
            cost_norm=s.cost_norm,
            rank_score=s.rank_score,
            rank=i + 1,
        )
        for i, s in enumerate(scored)
    ]
    return RankResult(group=group, ranked=ranked)
