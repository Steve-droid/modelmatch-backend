"""Project contract schemas (S8). camelCase out.

A project is created from a recommendation pick: the selected option (which model
+ harness to run) plus the baseline model savings are measured against. ProjectOut
is enriched with the model names so the FE can render without a second round-trip.
"""

from typing import Optional

from pydantic import Field

from app.schemas.base import CamelModel


class ProjectCreate(CamelModel):
    # Matches the DB column (String(200)); min_length rejects blank names cleanly.
    name: str = Field(min_length=1, max_length=200)
    selected_option_id: int
    baseline_model_id: int


class ProjectUpdate(CamelModel):
    """Partial edit (S15d): rename and/or re-pick the model + baseline. Every field
    is optional — a PATCH carries only what changed. A re-pick sends both
    `selectedOptionId` + `baselineModelId` (the FE re-runs the recommender first, so
    the new option belongs to the caller). An all-omitted body is a no-op."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    selected_option_id: Optional[int] = None
    baseline_model_id: Optional[int] = None


class ProjectOut(CamelModel):
    id: int
    name: str
    user_id: int  # owner
    selected_option_id: int
    selected_option_model: str  # the recommended model behind the selected option
    baseline_model_id: int
    baseline_model: str
    baseline_vendor: str
    # Fully onboarded = a Jenkins connection exists AND its CI ingest token was minted
    # (the user finished the wizard through /ci-setup). The FE badges the rest as
    # "setup incomplete" with an edit/retry path (S15d defer-create partial-failure).
    setup_complete: bool
