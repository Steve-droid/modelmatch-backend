"""Hard hourly token cap for OUR in-cluster Nova (Roey 2026-06-04).

A real ceiling that ABORTS, not an alert: ingestion (#3) — and later chat (#4) —
reserve their worst-case token estimate against a per-hour Postgres tally
(`llm_usage`) BEFORE calling the provider. If the reservation pushes the hour over
budget, it's released and the caller gets a 429 with no provider call (zero spend).
After the call, the reservation is reconciled to actual usage (usually a refund,
since the estimate budgets for full max output). On a provider error the reservation
is released too — a failed call must not burn the hour's budget.

Design notes:
- The tally is shared across replicas, so every mutation is a single atomic
  `INSERT … ON CONFLICT DO UPDATE` (add-and-return). Two replicas can't both slip
  under the cap. floored at 0 so a release can never drive the tally negative.
- This module is in-cluster only and DB-backed — deliberately NOT inside app/llm/
  (that package stays import-free of Settings/DB so the agent image stays
  standalone). The agent has its own per-run ceiling on the user's key.
- No FastAPI import here: callers translate HourlyTokenCapExceeded → 429.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import LlmUsage


class HourlyTokenCapExceeded(Exception):
    """The in-cluster hourly token budget would be exceeded — abort (→ 429)."""

    def __init__(self, *, used: int, requested: int, cap: int) -> None:
        self.used = used
        self.requested = requested
        self.cap = cap
        super().__init__(
            f"hourly token cap reached: {used} used + {requested} requested "
            f"> cap {cap} (aborted before the provider call)"
        )


@dataclass(frozen=True)
class Reservation:
    """A held estimate for the current hour bucket; reconcile/release settle it."""

    hour_start: datetime
    estimate: int


def _current_hour() -> datetime:
    """The current UTC hour, truncated — the `llm_usage` bucket key."""
    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _add(db: Session, hour_start: datetime, delta: int) -> int:
    """Atomically add `delta` to the hour's tally (floored at 0); return the new total.

    One statement so concurrent replicas can't race: ON CONFLICT folds the delta into
    the existing row. GREATEST(…, 0) keeps a release from underflowing the counter.
    """
    insert_val = delta if delta > 0 else 0
    stmt = (
        pg_insert(LlmUsage)
        .values(hour_start=hour_start, tokens_used=insert_val)
        .on_conflict_do_update(
            index_elements=["hour_start"],
            set_={"tokens_used": func.greatest(LlmUsage.tokens_used + delta, 0)},
        )
        .returning(LlmUsage.tokens_used)
    )
    total = db.execute(stmt).scalar_one()
    db.commit()
    return total


def reserve_tokens(db: Session, estimate: int, cap: int) -> Reservation:
    """Reserve `estimate` tokens for this hour; raise (and release) if it busts `cap`.

    Returns a Reservation to settle once the real usage is known. The check is on the
    *post-reservation* total, so a call is admitted only if it fits entirely.
    """
    hour = _current_hour()
    new_total = _add(db, hour, estimate)
    if new_total > cap:
        _add(db, hour, -estimate)  # release — we will not make the call
        raise HourlyTokenCapExceeded(
            used=new_total - estimate, requested=estimate, cap=cap
        )
    return Reservation(hour_start=hour, estimate=estimate)


def reconcile(db: Session, reservation: Reservation, actual_tokens: int) -> int:
    """Settle a reservation to actual usage after the call. Returns the new total."""
    return _add(db, reservation.hour_start, actual_tokens - reservation.estimate)


def release(db: Session, reservation: Reservation) -> int:
    """Fully release a reservation (e.g. the provider call failed). New total."""
    return _add(db, reservation.hour_start, -reservation.estimate)
