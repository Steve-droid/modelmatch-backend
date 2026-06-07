"""ci token hash + ci_run build uniqueness + gate audit trail

S11 (run-ingest):
- jenkins_connection.ci_token_hash — SHA-256 hex of the per-project CI ingest
  token (NEVER plaintext); minted once at GET /ci-setup, verified on POST /ci-runs.
- ci_run unique (project_id, jenkins_build_id) — a re-POSTed build is rejected
  (409). NULLs stay distinct so non-ingest inserts may omit the build id.
- ci_run.gate / ci_run.gate_reason — audit trail of the agent's pass/fail decision
  (the gate acts in the user's CI; the backend keeps the record).

First migration since the catalog constraints (0002). Mirrors the ORM exactly so
the autogenerate-no-drift test stays green.

Revision ID: c1d2e3f4a5b6
Revises: ae527d20dc5a
Create Date: 2026-06-07 14:40:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, None] = 'ae527d20dc5a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jenkins_connection",
        sa.Column(
            "ci_token_hash",
            sa.String(length=64),
            nullable=True,
            comment="SHA-256 hash of the per-project CI ingest token — NEVER plaintext",
        ),
    )
    op.create_unique_constraint(
        "uq_ci_run_project_build", "ci_run", ["project_id", "jenkins_build_id"]
    )
    op.add_column(
        "ci_run",
        sa.Column(
            "gate",
            sa.String(length=16),
            nullable=True,
            comment="agent pass/fail audit trail (the gate acts in the user's CI)",
        ),
    )
    op.add_column("ci_run", sa.Column("gate_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ci_run", "gate_reason")
    op.drop_column("ci_run", "gate")
    op.drop_constraint("uq_ci_run_project_build", "ci_run", type_="unique")
    op.drop_column("jenkins_connection", "ci_token_hash")
