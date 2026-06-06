"""Deterministic scoring — the heart of the recommender (NO LLM, NO embeddings).

A pure function of (catalog rows, weight): same inputs → same output. The LLM
never ranks; this transparent formula does. From architecture §3.1:

    within a comparability group (same benchmark + same metric):
      quality = score / max_score_in_group        # 0..1
      cost    = cost_per_mtok / max_cost_in_group  # 0..1
      rank_score = w_q · quality + w_c · (1 − cost) # w_q + w_c = 1

**Comparability rule (hard):** rows are only ever normalized against others in
the same (benchmark, metric) group — never Pass@1 against accuracy. When the
filtered rows span more than one group, we rank within the single *dominant*
group (most rows) and report which group was used; cross-group rank_scores
aren't comparable (different normalization bases), so we never merge them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# rank_score / normalized values are quantized to 6 dp so persisted Numeric(14,6)
# values and in-memory comparisons agree exactly (determinism, not float drift).
_Q = Decimal("0.000001")
_ZERO = Decimal(0)
_ONE = Decimal(1)


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

    # Pick the dominant (benchmark, metric) group deterministically: most rows,
    # ties → lexicographically smallest key.
    counts: dict[tuple[str, str], int] = {}
    for it in items:
        counts[_group_key(it)] = counts.get(_group_key(it), 0) + 1
    group = min(counts, key=lambda k: (-counts[k], k))

    rows = [it for it in items if _group_key(it) == group]
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
