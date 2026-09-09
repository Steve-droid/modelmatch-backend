"""Operator entitlement and replica-shared auth admission (P38o)."""
from alembic import op
import sqlalchemy as sa

revision = "f3a4b5c6d7e8"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user", sa.Column("is_operator", sa.Boolean(), nullable=False,
                                    server_default=sa.false()))
    op.create_table(
        "auth_rate_bucket",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tokens", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="single_auth_bucket"),
    )


def downgrade():
    # Downgrading code to an unrestricted version is unsafe; see the rollout runbook.
    op.drop_table("auth_rate_bucket")
    op.drop_column("user", "is_operator")
