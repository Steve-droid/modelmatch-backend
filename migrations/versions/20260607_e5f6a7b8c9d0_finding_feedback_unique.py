"""finding_feedback uniqueness (S13): one verdict per (finding, user)

The quality gate's invariant is one accept/reject verdict per (finding, user). S13
enforces it in the DB rather than in app code: a query-then-insert "upsert" is a
TOCTOU race — two parallel POSTs (multiple k8s replicas) can both miss the existing
row and double-insert, corrupting the acceptance rate. This unique constraint is also
the ON CONFLICT target the feedback path upserts against (atomic, concurrency-safe).

NULLS NOT DISTINCT (PG15+) so a NULL user_id still dedupes, matching the codebase's
other natural keys. Mirrors the ORM (FindingFeedback.__table_args__) so the
autogenerate-no-drift parity test stays green.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-07 20:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_finding_feedback_finding_user",
        "finding_feedback",
        ["ci_finding_id", "user_id"],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_finding_feedback_finding_user", "finding_feedback", type_="unique"
    )
