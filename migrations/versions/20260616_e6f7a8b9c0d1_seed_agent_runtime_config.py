"""seed trusted agent runtime config rows

Ensure the supported CI-agent runtime mappings exist as trusted application
configuration, independent of whether the offline test seed has been run.

The migration is idempotent:
- create the three supported `model` dimension rows if they do not exist yet
- upsert the matching `agent_runtime_config` rows by `model_id`

Revision ID: e6f7a8b9c0d1
Revises: c2d3e4f5a6b7
Create Date: 2026-06-16 23:10:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SUPPORTED_MODELS = (
    ("Claude Haiku 4.5", "Anthropic"),
    ("Nova 2 Lite", "Amazon"),
    ("Gemini 2.5 Flash", "Google"),
)

SUPPORTED_RUNTIME_CONFIGS = (
    (
        "Claude Haiku 4.5",
        "Anthropic",
        "anthropic",
        "claude-haiku-4-5",
        "api_key",
        "ANTHROPIC_API_KEY",
    ),
    (
        "Nova 2 Lite",
        "Amazon",
        "bedrock",
        "global.amazon.nova-2-lite-v1:0",
        "aws_iam",
        None,
    ),
    (
        "Gemini 2.5 Flash",
        "Google",
        "gemini",
        "gemini-2.5-flash",
        "api_key",
        "GOOGLE_API_KEY",
    ),
)


def upgrade() -> None:
    bind = op.get_bind()

    for name, vendor in SUPPORTED_MODELS:
        bind.execute(
            sa.text(
                """
                INSERT INTO model (name, vendor)
                VALUES (:name, :vendor)
                ON CONFLICT (name, vendor) DO NOTHING
                """
            ),
            {"name": name, "vendor": vendor},
        )

    for model_name, vendor, provider, provider_model_id, auth_mode, credential_env_var in SUPPORTED_RUNTIME_CONFIGS:
        bind.execute(
            sa.text(
                """
                INSERT INTO agent_runtime_config
                    (model_id, provider, provider_model_id, auth_mode, credential_env_var, enabled)
                SELECT
                    m.id,
                    CAST(:provider AS agent_provider),
                    :provider_model_id,
                    CAST(:auth_mode AS agent_auth_mode),
                    :credential_env_var,
                    TRUE
                FROM model AS m
                WHERE m.name = :model_name
                  AND m.vendor = :vendor
                ON CONFLICT (model_id) DO UPDATE SET
                    provider = EXCLUDED.provider,
                    provider_model_id = EXCLUDED.provider_model_id,
                    auth_mode = EXCLUDED.auth_mode,
                    credential_env_var = EXCLUDED.credential_env_var,
                    enabled = EXCLUDED.enabled
                """
            ),
            {
                "model_name": model_name,
                "vendor": vendor,
                "provider": provider,
                "provider_model_id": provider_model_id,
                "auth_mode": auth_mode,
                "credential_env_var": credential_env_var,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()

    for name, vendor in SUPPORTED_MODELS:
        bind.execute(
            sa.text(
                """
                DELETE FROM agent_runtime_config
                WHERE model_id IN (
                    SELECT id FROM model WHERE name = :name AND vendor = :vendor
                )
                """
            ),
            {"name": name, "vendor": vendor},
        )

