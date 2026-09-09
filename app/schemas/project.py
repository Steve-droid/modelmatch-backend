"""Project contract schemas (S8, E20). camelCase out.

A project is created from a recommendation pick: the selected option (which model
+ harness to run) plus the baseline model savings are measured against — and, since
E20, the ONE task the agent runs (`taskType`) plus optional review preferences.
ProjectOut is enriched with the model names so the FE can render without a second
round-trip.
"""

from typing import Optional

from pydantic import Field, field_validator

from app.schemas.base import CamelModel
from app.tasks import REVIEW_PREFERENCES_MAX_LEN, TaskType


def _clean_preferences(v: Optional[str]) -> Optional[str]:
    """Blank/whitespace-only preferences mean "none". Length is bounded by the field;
    control characters are stripped by the agent before prompting."""
    if v is None:
        return None
    v = v.strip()
    return v or None


class ProjectCreate(CamelModel):
    # Matches the DB column (String(200)); min_length rejects blank names cleanly.
    name: str = Field(min_length=1, max_length=200)
    selected_option_id: int
    baseline_model_id: int
    # The task the agent will run. Optional on the wire for older clients: when
    # omitted it is DERIVED from the selected option's recommendation; when given it
    # must MATCH that recommendation (an option ranked on RealVuln cannot back a
    # review project) — the service 422s a mismatch.
    task_type: Optional[TaskType] = None
    # Review task only (bounded); stored as given, served to the agent by agent-config.
    review_preferences: Optional[str] = Field(
        default=None, max_length=REVIEW_PREFERENCES_MAX_LEN
    )

    _clean_prefs = field_validator("review_preferences")(_clean_preferences)


class ProjectUpdate(CamelModel):
    """Partial edit (S15d): rename and/or re-pick the model + baseline, and (E20) edit
    the review preferences. Every field is optional — a PATCH carries only what
    changed. A re-pick sends both `selectedOptionId` + `baselineModelId` (the FE
    re-runs the recommender first, so the new option belongs to the caller) and may
    send `taskType` (it must match the new option). `reviewPreferences: null` clears
    them. An all-omitted body is a no-op."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    selected_option_id: Optional[int] = None
    baseline_model_id: Optional[int] = None
    task_type: Optional[TaskType] = None
    review_preferences: Optional[str] = Field(
        default=None, max_length=REVIEW_PREFERENCES_MAX_LEN
    )

    _clean_prefs = field_validator("review_preferences")(_clean_preferences)


class ProjectOut(CamelModel):
    id: int
    name: str
    user_id: int  # owner
    selected_option_id: int
    selected_option_model: str  # the recommended model behind the selected option
    baseline_model_id: int
    baseline_model: str
    baseline_vendor: str
    # E20: the task this project's agent runs (catalog vocabulary) + the review
    # preferences the review agent appends to its prompt (null when none / security).
    task_type: str
    review_preferences: Optional[str] = None
    # Fully onboarded = a Jenkins connection exists AND its CI ingest token was minted
    # (the user finished the wizard through /ci-setup). The FE badges the rest as
    # "setup incomplete" with an edit/retry path (S15d defer-create partial-failure).
    setup_complete: bool
    is_example: bool = False
