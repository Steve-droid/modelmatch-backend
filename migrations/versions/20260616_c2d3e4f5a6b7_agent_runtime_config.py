"""agent runtime config table

Runtime provider metadata is trusted application configuration, separate from the
LLM-populated catalog rows. The catalog still decides which model row is selected;
this table maps that model to the concrete provider/model/auth details the CI agent
needs when generating BYOK Jenkins runtime configuration.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-06-16 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg


# revision identifiers, used by Alembic.
revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


agent_provider = pg.ENUM(
    "anthropic", "gemini", "bedrock", name="agent_provider", create_type=False
)
agent_auth_mode = pg.ENUM("api_key", "aws_iam", name="agent_auth_mode", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    agent_provider.create(bind, checkfirst=True)
    agent_auth_mode.create(bind, checkfirst=True)

    op.create_table(
        "agent_runtime_config",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "model_id",
            sa.Integer,
            sa.ForeignKey("model.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", agent_provider, nullable=False),
        sa.Column("provider_model_id", sa.String(255), nullable=False),
        sa.Column("auth_mode", agent_auth_mode, nullable=False),
        sa.Column("credential_env_var", sa.String(128), nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("model_id", name="uq_agent_runtime_config_model_id"),
    )


def downgrade() -> None:
    op.drop_table("agent_runtime_config")
    bind = op.get_bind()
    agent_auth_mode.drop(bind, checkfirst=True)
    agent_provider.drop(bind, checkfirst=True)
