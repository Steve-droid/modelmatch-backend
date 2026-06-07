"""Quality gate (S13): the honesty guardrail on the savings number.

S12 ships per-run savings but counts *every* run. S13 makes the headline honest: a
human gives each CI finding an **accept/reject** verdict, the run's **acceptance
rate** = accepted / rated findings, and the run only banks its savings when that
rate clears `QUALITY_THRESHOLD`. Sub-threshold runs are excluded from the cumulative
AND surfaced as "quality risk" (architecture §4, §8) — never silently dropped.

DETERMINISTIC — pure arithmetic over human verdicts, NO LLM, zero tokens (same
discipline as the recommender / savings engine). Same inputs → same output.

Two layers:
- The PURE core here (`acceptance_rate`, `recompute_quality`) — unit-tested with
  known numbers, no DB.
- The wiring (`submit_feedback`) — persists one verdict per (finding, user) and
  recomputes the affected run's stored `quality_ok`.

Locked decisions (S13 forks):
- Denominator = accepted / **rated** (OQ1): you can only judge what's been reviewed;
  an unreviewed finding is "not yet gated," not "rejected."
- Empty / unrated run → `quality_ok` **NULL** (un-gated): excluded from the honest
  cumulative, counts neither for nor against — surfaced as "unrated," distinct from
  "quality risk."
- Scope = **per-run** `quality_ok`, computed from that run's own findings' feedback.
- Recompute **on each feedback POST** (store it on the run, like S12 stores savings).
- One verdict per (finding, user), **upsert** — latest wins.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.auth.deps import require_owner
from app.config import get_settings
from app.models import CiFinding, CiRun, FindingFeedback, Project, User
from app.schemas.feedback import FeedbackIn, FeedbackOut

_ACCEPT = "accept"


# --- pure core: known verdicts → rate / gate -------------------------------

def acceptance_rate(verdicts: Iterable[str]) -> Optional[float]:
    """Fraction of *rated* findings that were accepted, or None if none are rated.

    Denominator = rated findings only (OQ1). `verdicts` is the list of accept/reject
    verdicts for the findings that have feedback; unrated findings are simply absent.
    Empty → None (the run is un-gated, not failed).
    """
    verdicts = list(verdicts)
    if not verdicts:
        return None
    accepted = sum(1 for v in verdicts if v == _ACCEPT)
    return accepted / len(verdicts)


def recompute_quality(verdicts: Iterable[str], threshold: float) -> Optional[bool]:
    """The per-run quality gate: `quality_ok = acceptance_rate ≥ threshold`.

    Returns None when nothing is rated yet (rate is undefined → NULL = un-gated),
    else True/False. Threshold is env-driven (`QUALITY_THRESHOLD`), never hardcoded.
    """
    rate = acceptance_rate(verdicts)
    if rate is None:
        return None
    return rate >= threshold


# --- wiring: persist a verdict + recompute the run's quality_ok ------------

def _run_verdicts(db: Session, ci_run_id: int) -> list[str]:
    """Every feedback verdict on a run's findings (one per finding after upsert)."""
    return list(
        db.scalars(
            select(FindingFeedback.verdict)
            .join(CiFinding, FindingFeedback.ci_finding_id == CiFinding.id)
            .where(CiFinding.ci_run_id == ci_run_id)
        ).all()
    )


def submit_feedback(
    db: Session, finding_id: int, payload: FeedbackIn, current_user: User
) -> FeedbackOut:
    """Record an accept/reject verdict on a finding, then recompute its run's gate.

    User-JWT + owner-scoped via the finding → run → project → user chain (404 if the
    finding is missing, 403 if the project isn't the caller's). One verdict per
    (finding, user): re-feedback **upserts** (latest wins), never duplicates. After
    persisting, the affected run's `quality_ok` is recomputed from all its findings'
    verdicts and stored — DETERMINISTIC, no LLM, zero tokens.
    """
    finding = db.get(CiFinding, finding_id)
    if finding is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Finding not found"
        )

    # Serialize feedback on the SAME run: lock its row FOR UPDATE before we
    # upsert + recompute. quality_ok is a read-modify-write (read all verdicts →
    # write the gate), so two parallel POSTs on DIFFERENT findings of one run could
    # each recompute from a PARTIAL verdict set and clobber each other (last commit
    # wins → a stale gate until the next feedback). The row lock makes the second
    # txn block here until the first commits, then re-read the full set. (The upsert
    # below is already atomic; this guards the aggregate, not the row.)
    run = db.scalar(
        select(CiRun).where(CiRun.id == finding.ci_run_id).with_for_update()
    )
    project = db.get(Project, run.project_id)
    require_owner(project.user_id, current_user)  # 403 if not the caller's

    # Upsert one verdict per (finding, user) ATOMICALLY via ON CONFLICT on the
    # uq_finding_feedback_finding_user constraint: insert, or update the verdict if the
    # caller already rated this finding (latest wins). This is concurrency-safe —
    # parallel POSTs across replicas can't double-insert (a query-then-insert would
    # race). Done at the DB, not in app code, so the invariant holds under contention.
    db.execute(
        pg_insert(FindingFeedback)
        .values(
            ci_finding_id=finding_id,
            verdict=payload.verdict,
            user_id=current_user.id,
        )
        .on_conflict_do_update(
            constraint="uq_finding_feedback_finding_user",
            set_={"verdict": payload.verdict},
        )
    )

    threshold = get_settings().quality_threshold
    verdicts = _run_verdicts(db, run.id)
    run.quality_ok = recompute_quality(verdicts, threshold)
    db.commit()

    return FeedbackOut(
        finding_id=finding_id,
        ci_run_id=run.id,
        verdict=payload.verdict,
        acceptance_rate=acceptance_rate(verdicts),
        quality_ok=run.quality_ok,
    )
