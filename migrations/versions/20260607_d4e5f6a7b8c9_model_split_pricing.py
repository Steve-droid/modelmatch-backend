"""model split pricing (S12 revision): input vs output per-MTok prices

S12 originally costed runs with a single blended `model.price_per_mtok`, but real
LLM APIs price input and output tokens differently (e.g. Claude Sonnet 4.x = $3/MTok
in, $15/MTok out). This adds the two split-price columns the savings engine now uses:

- model.input_price_per_mtok  — per-MTok price for prompt/input tokens
- model.output_price_per_mtok — per-MTok price for completion/output tokens

Both nullable (a model may be unpriced → savings stays NULL). The legacy
`model.price_per_mtok` column is KEPT as a blended display/ranking price (recommender
sorting), but is no longer read by the savings engine. Nullable + additive, so no
backfill DDL is needed (the catalog upsert populates them, backfilling old/partial
rows from the blended price). Mirrors the ORM so autogenerate-no-drift stays green.

Revision ID: d4e5f6a7b8c9
Revises: c1d2e3f4a5b6
Create Date: 2026-06-07 18:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "model",
        sa.Column("input_price_per_mtok", sa.Numeric(precision=14, scale=6), nullable=True),
    )
    op.add_column(
        "model",
        sa.Column("output_price_per_mtok", sa.Numeric(precision=14, scale=6), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("model", "output_price_per_mtok")
    op.drop_column("model", "input_price_per_mtok")
