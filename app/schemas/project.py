"""Project contract schemas (S8). camelCase out.

A project is created from a recommendation pick: the selected option (which model
+ harness to run) plus the baseline model savings are measured against. ProjectOut
is enriched with the model names so the FE can render without a second round-trip.
"""

from pydantic import Field

from app.schemas.base import CamelModel


class ProjectCreate(CamelModel):
    # Matches the DB column (String(200)); min_length rejects blank names cleanly.
    name: str = Field(min_length=1, max_length=200)
    selected_option_id: int
    baseline_model_id: int


class ProjectOut(CamelModel):
    id: int
    name: str
    user_id: int  # owner
    selected_option_id: int
    selected_option_model: str  # the recommended model behind the selected option
    baseline_model_id: int
    baseline_model: str
    baseline_vendor: str
