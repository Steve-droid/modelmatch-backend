"""ci_run.created_at (S14): a timestamp for the savings dashboard's time axis

The savings dashboard (S14) charts actual-vs-baseline "over time", reports "spend
this period" + "projected monthly", and accepts `?range=`. All of that needs a date
to order/bucket by — but ci_run had no timestamp (only a monotonic id). This adds
`created_at` (timestamptz, DB-assigned on insert) so the series/KPIs have a real time
axis. Mirrors chat_message.created_at exactly (server_default now()) so the
autogenerate-no-drift + round-trip parity tests stay green.

Pure additive column with a server default — existing rows backfill to now() at apply
time; no data migration needed.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-08 10:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ci_run",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("ci_run", "created_at")
