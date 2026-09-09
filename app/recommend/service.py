"""Recommender service: filter the catalog, rank deterministically, persist.

Bridges the pure scoring core (app.recommend.scoring) to the DB. It reads the
catalog, runs score_and_rank within the dominant comparability group, chooses a
baseline, and persists the form (requirements_profile) + the ranked options
(recommendation_option) + their provenance (recommendation_evidence), owner-scoped
to the requesting user. Still NO LLM: the LLM fills the catalog, the formula ranks.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AgentRuntimeConfig,
    BenchmarkResult,
    Model,
    RecommendationEvidence,
    RecommendationOption,
    RequirementsProfile,
    User,
)
from app.recommend.scoring import (
    MultipleComparabilityGroups,
    RankedItem,
    ScoreInput,
    score_and_rank,
)
from app.schemas.recommend import (
    BaselineOut,
    ComparabilityGroup,
    RecommendationOptionOut,
    RecommendationRequest,
    RecommendationResult,
)


def _weight_for(budget_sensitivity: str) -> Decimal:
    """Map the budget-sensitivity preset to the quality weight w_q (env-tunable)."""
    s = get_settings()
    presets = {
        "low": s.rank_weight_low,
        "medium": s.rank_weight_medium,
        "high": s.rank_weight_high,
    }
    return Decimal(str(presets[budget_sensitivity]))


def _to_option_out(
    item: RankedItem, option_id: int, model_id: int, vendor: str
) -> RecommendationOptionOut:
    return RecommendationOptionOut(
        recommendation_option_id=option_id,
        rank=item.rank,
        model=item.model,
        model_id=model_id,
        vendor=vendor,
        benchmark=item.benchmark,
        metric=item.metric,
        score=item.score,
        cost_per_mtok=item.cost,
        quality_norm=item.quality,
        cost_norm=item.cost_norm,
        rank_score=item.rank_score,
        benchmark_result_id=item.key,
    )


def recommend(
    db: Session, req: RecommendationRequest, current_user: User
) -> RecommendationResult:
    # ① filter the catalog by the selected task type(s). Only rows with both a score
    # and a cost are rankable.
    rows = db.scalars(
        select(BenchmarkResult)
        .join(Model, BenchmarkResult.model_id == Model.id)
        .where(BenchmarkResult.task_type.in_(req.task_types))
    ).all()
    candidates: dict[int, BenchmarkResult] = {
        r.id: r for r in rows if r.score is not None and r.cost_per_mtok is not None
    }

    # ①b restrict to models the pipeline can actually reach (P38c). Modicum's
    # output is a model to deploy, so a row with no enabled agent_runtime_config — no
    # credential the generated snippet can inject — is not a recommendation we can
    # honour. Benchmarks score plenty of models the agent has no verified path to
    # (RealVuln measures DeepSeek, Moonshot, Z.AI and local open-weight builds);
    # ranking those would produce a confident pick the user cannot run. They stay in
    # the catalog and answerable by the chat, just out of the pick.
    #
    # A SUPPORT boundary, not a possession one — the key is always the user's, and this
    # table only records that the agent can drive the model and which env var carries
    # that key. For the security task the runtime (OpenCode) already speaks most of
    # these providers, so the set widens with a verification run, not adapter code.
    by_id = candidates
    if get_settings().recommend_only_runnable:
        runnable_model_ids = set(
            db.scalars(
                select(AgentRuntimeConfig.model_id).where(AgentRuntimeConfig.enabled.is_(True))
            ).all()
        )
        runnable = {
            rid: r for rid, r in candidates.items() if r.model_id in runnable_model_ids
        }
        # Never rank an empty set: if this task has scores but none are runnable, fall
        # back to the full candidate set rather than 422-ing the user out of a pick.
        # The counts on the response still show what happened.
        by_id = runnable or candidates

    inputs = [
        ScoreInput(
            key=r.id,
            model=r.model.name,
            benchmark=r.benchmark.name,
            metric=r.metric or "",
            score=r.score,
            cost=r.cost_per_mtok,
        )
        for r in by_id.values()
    ]

    # ② weighted score within the task's single comparability group. Since P38c a
    # task type owns exactly one (benchmark, metric) pair, so selecting task types
    # measured by different benchmarks has no comparable ranking — surface that
    # instead of ranking whichever group happened to have more rows.
    try:
        result = score_and_rank(inputs, _weight_for(req.budget_sensitivity))
    except MultipleComparabilityGroups as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    if not result.ranked:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="No catalog rows match the selected task types.",
        )

    settings = get_settings()
    shortlist = result.ranked[: settings.recommendation_shortlist_size]

    # ③ baseline: the model configured FOR THIS TASK (P38c — each task is measured
    # by its own benchmark and deserves its own "expensive default": Sonnet 4.5 for
    # code review, Opus 5 for security analysis). Falls back to the highest-cost row
    # in the group when the configured name is not a scored row here.
    baseline_item, baseline_selection = _pick_baseline(
        result.ranked, settings.baseline_for(_task_type_of(by_id, result.ranked, req))
    )

    # Persist: profile (owner-scoped) → options → evidence. Capture model_id/vendor
    # and the persisted option ids BEFORE commit, so the response is built from
    # persisted rows (not just the pure RankedItem) without post-commit reloads.
    profile = RequirementsProfile(
        user_id=current_user.id,
        task_types=list(req.task_types),
        budget_sensitivity=req.budget_sensitivity,
        latency_need=req.latency_need,
    )
    db.add(profile)
    db.flush()
    profile_id = profile.id

    option_id_by_key: dict[int, int] = {}
    meta_by_key: dict[int, tuple[int, str]] = {}  # key → (model_id, vendor)
    for item in shortlist:
        br = by_id[item.key]
        meta_by_key[item.key] = (br.model_id, br.model.vendor)
        option = RecommendationOption(
            profile_id=profile_id,
            rank=item.rank,
            rank_score=item.rank_score,
            model_id=br.model_id,
            harness_id=br.harness_id,
        )
        db.add(option)
        db.flush()
        option_id_by_key[item.key] = option.id
        db.add(
            RecommendationEvidence(
                recommendation_option_id=option.id,
                benchmark_result_id=br.id,
            )
        )

    baseline_br = by_id[baseline_item.key]
    baseline = BaselineOut(
        model=baseline_item.model,
        model_id=baseline_br.model_id,
        vendor=baseline_br.model.vendor,
        cost_per_mtok=baseline_item.cost,
        benchmark_result_id=baseline_item.key,
        selection=baseline_selection,
    )
    db.commit()

    options_out = [
        _to_option_out(item, option_id_by_key[item.key], *meta_by_key[item.key])
        for item in shortlist
    ]
    return RecommendationResult(
        profile_id=profile_id,
        comparability_group=ComparabilityGroup(
            benchmark=result.group[0],
            metric=result.group[1],
            ranked_count=len(result.ranked),
            candidate_count=len(candidates),
        ),
        suggested=options_out[0],
        baseline=baseline,
        shortlist=options_out,
    )


def _task_type_of(
    by_id: dict[int, BenchmarkResult],
    ranked: list[RankedItem],
    req: RecommendationRequest,
) -> str | None:
    """The task type the ranking actually happened in.

    Taken from the ranked rows rather than the request, so the baseline follows the
    data that was scored. (Post-P38c these agree: one task type, one group.) Falls
    back to the requested task type when a row carries no task_type.
    """
    for item in ranked:
        task_type = by_id[item.key].task_type
        if task_type:
            return task_type
    return req.task_types[0] if req.task_types else None


def _pick_baseline(
    group_items: list[RankedItem], configured_name: str
) -> tuple[RankedItem, str]:
    """Pick the baseline row: configured model name within the group, else the
    highest-cost row. Returns (item, selection) — the caller resolves model_id/vendor."""
    configured = [i for i in group_items if i.model == configured_name]
    if configured:
        # one representative row for that model (deterministic: lowest id)
        return min(configured, key=lambda i: i.key), "configured"
    # fallback: most expensive model in the group (tie-break: lowest id)
    return max(group_items, key=lambda i: (i.cost, -i.key)), "fallback_highest_cost"
