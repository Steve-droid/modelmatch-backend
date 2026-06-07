"""Finding-feedback contract (S13): the quality-signal verdict.

A human reviews a CI finding and says whether it was a real, useful catch
(`accept`) or noise (`reject`). The per-run acceptance rate of these verdicts gates
whether the run's savings count toward the honest cumulative (architecture §4, §8).

camelCase out (CamelModel). `verdict` is a Literal so a bad value is a clean 422,
and `extra="forbid"` rejects any unexpected field.
"""

from typing import Literal, Optional

from pydantic import ConfigDict

from app.schemas.base import CamelModel

Verdict = Literal["accept", "reject"]


class FeedbackIn(CamelModel):
    """The user's verdict on one finding. Only `verdict` is accepted."""

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict


class FeedbackOut(CamelModel):
    """The recorded verdict + the run's recomputed gate (camelCase out).

    `acceptance_rate` is the run's accepted/rated fraction after this verdict (None if
    nothing is rated — un-gated); `quality_ok` is the stored per-run gate (None when
    un-gated, else rate ≥ threshold).
    """

    finding_id: int
    ci_run_id: int
    verdict: Verdict
    acceptance_rate: Optional[float] = None
    quality_ok: Optional[bool] = None
