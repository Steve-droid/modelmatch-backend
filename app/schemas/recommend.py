"""Recommender contract schemas (S6 pick + S7 pre-fill). camelCase out.

The deterministic recommender's wire shapes: the form request, the ranked result
(suggested option + baseline + shortlist, each carrying its rank_score and the
benchmark_result that informed it), and the keyword pre-fill request/response.
No LLM anywhere on this path.
"""

from decimal import Decimal
from typing import Literal, Optional

from pydantic import Field

from app.schemas.base import CamelModel

BudgetSensitivity = Literal["low", "medium", "high"]
LatencyNeed = Literal["low", "medium", "high"]


class RecommendationRequest(CamelModel):
    """Form inputs. budgetSensitivity tunes the quality↔cost weights; latencyNeed
    is captured for later use (it does not affect S6 scoring)."""

    task_types: list[str] = Field(min_length=1)
    budget_sensitivity: BudgetSensitivity = "medium"
    latency_need: Optional[LatencyNeed] = None


class ComparabilityGroup(CamelModel):
    """The (benchmark, metric) group the ranking was computed within — surfaced so
    the user can see rows were only compared like-for-like."""

    benchmark: str
    metric: str
    # P38c transparency: how many catalog rows this task has, and how many were
    # ranked. They differ when rows were excluded as not runnable by the CI agent
    # (RECOMMEND_ONLY_RUNNABLE), so the UI can say "ranked 2 of 16 models your agent
    # can run" instead of silently presenting a shortened list.
    ranked_count: Optional[int] = None
    candidate_count: Optional[int] = None


class RecommendationOptionOut(CamelModel):
    recommendation_option_id: int  # persisted recommendation_option.id — S8 selects this
    rank: int
    model: str
    model_id: int
    vendor: str
    benchmark: str
    metric: str
    score: Optional[Decimal] = None
    cost_per_mtok: Optional[Decimal] = None
    quality_norm: Decimal  # score / max_score_in_group
    cost_norm: Decimal  # cost / max_cost_in_group
    rank_score: Decimal
    benchmark_result_id: int  # the evidence row that informed this option


class BaselineOut(CamelModel):
    """The 'expensive default' savings are measured against (computed, never run).

    A model reference (not a persisted option) — S8 stores it as project.baseline_model_id.
    """

    model: str
    model_id: int
    vendor: str
    cost_per_mtok: Optional[Decimal] = None
    benchmark_result_id: Optional[int] = None
    selection: Literal["configured", "fallback_highest_cost"]


class RecommendationResult(CamelModel):
    profile_id: int
    comparability_group: Optional[ComparabilityGroup] = None
    suggested: RecommendationOptionOut
    baseline: BaselineOut
    shortlist: list[RecommendationOptionOut]


class PrefillRequest(CamelModel):
    text: str


class PrefillResult(CamelModel):
    """Suggested form fields from free text. The user CONFIRMS — never auto-submitted.
    matchedTerms shows which keywords fired (transparency; deterministic, no LLM)."""

    task_types: list[str] = Field(default_factory=list)
    budget_sensitivity: Optional[BudgetSensitivity] = None
    latency_need: Optional[LatencyNeed] = None
    matched_terms: list[str] = Field(default_factory=list)
