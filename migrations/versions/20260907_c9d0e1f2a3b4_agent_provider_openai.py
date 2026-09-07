"""add 'openai' to the agent_provider enum

P38h. RealVuln scores GPT-5.5 at F3 56.7 for $5/$30 under the OpenCode harness — the
highest security score in the catalog — but the recommender may only pick a model with
an enabled `agent_runtime_config` (P38c, RECOMMEND_ONLY_RUNNABLE), and that table's
`provider` column is a native Postgres enum that did not include OpenAI. Same shape as
P38g (deepseek): the security runtime is the OpenCode CLI, which speaks OpenAI natively,
so no adapter code is needed — only a value in this type plus the seed row that
accompanies it in `app/catalog/seed_data.json`. OpenAI stays out of the in-cluster
surface and out of the review task (no CodeReviewBench row): this enables the
security_analysis task only.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-09-07 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Postgres 16 permits ALTER TYPE ... ADD VALUE inside a transaction but forbids
    # USING the new value until that transaction commits — and migrations/env.py wraps
    # the entire run in a single context.begin_transaction(). autocommit_block() commits
    # what is open, runs the ALTER outside a transaction, then reopens one, so the value
    # is usable immediately: by a later revision, by the catalog seed, by the app.
    # IF NOT EXISTS keeps a re-run (or a hand-patched database) idempotent.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE agent_provider ADD VALUE IF NOT EXISTS 'openai'")


def downgrade() -> None:
    # Postgres cannot drop a value from an enum, so a real downgrade rebuilds the type.
    # Rows still on 'openai' must go first or the USING cast below fails with
    # "invalid input value for enum agent_provider". Those rows are trusted static
    # config, not user data — `python -m app.catalog.seed` recreates them — so removing
    # them is recoverable, whereas a downgrade that aborts against a seeded database
    # (the live stack, not just a fresh test DB) is not a downgrade at all.
    op.execute("DELETE FROM agent_runtime_config WHERE provider = 'openai'")

    op.execute("ALTER TYPE agent_provider RENAME TO agent_provider_old")
    op.execute(
        "CREATE TYPE agent_provider AS ENUM ('anthropic', 'gemini', 'bedrock', 'deepseek')"
    )
    # agent_runtime_config.provider is the only column on this type and carries no
    # server_default, so there is no default to drop and restore around the cast.
    op.execute(
        "ALTER TABLE agent_runtime_config "
        "ALTER COLUMN provider TYPE agent_provider "
        "USING provider::text::agent_provider"
    )
    op.execute("DROP TYPE agent_provider_old")
