"""two tasks: project.task_type + review_preferences, ci_finding.cwe, ci_run.cache_read_tokens

P38e (E20). The agent image now runs one of TWO tasks per project — `ci_review` (one
call over the PR diff) or `security_analysis` (an agentic scan of the checkout) — and
fetches which one from `GET /projects/{id}/agent-config` (HLD §3b.1). Until now a
project's task was only implied by the recommendation it was built from; the agent
needs it stated. So:

  * `project.task_type`          — NOT NULL, the catalog vocabulary. Existing projects
                                   are backfilled from their selected option's
                                   requirements profile (the task they were ranked
                                   on); anything unresolvable keeps the default.
  * `project.review_preferences` — bounded free text the review agent appends to its
                                   prompt (review task only; the API returns null for
                                   security projects).
  * `ci_finding.cwe`             — the security task's CWE per finding, e.g.
                                   "CWE-89: SQL Injection". Seeded security findings
                                   already spell it at the head of `message`, so that
                                   prefix is lifted into the column for live rows.
  * `ci_run.cache_read_tokens`   — the agentic loop's cache-read tokens, STORED, never
                                   priced (HLD §8 keeps that a known limitation).
  * `ci_run.task`                — the legacy literal `code_review` becomes `ci_review`
                                   so the run, the project and the catalog share one
                                   vocabulary (the chat grounding names the task).

Revision ID: d1e2f3a4b5c6
Revises: c9d0e1f2a3b4
Create Date: 2026-09-08 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "project",
        sa.Column(
            "task_type",
            sa.String(length=64),
            nullable=False,
            server_default="ci_review",
            comment="the task this project's agent runs: ci_review | security_analysis",
        ),
    )
    op.add_column(
        "project",
        sa.Column(
            "review_preferences",
            sa.Text(),
            nullable=True,
            comment="review task only: bounded text appended to the agent's prompt",
        ),
    )
    op.add_column(
        "ci_run",
        sa.Column(
            "cache_read_tokens",
            sa.Integer(),
            nullable=True,
            comment="agentic-loop cache-read tokens — stored for the record, never priced",
        ),
    )
    op.add_column(
        "ci_finding",
        sa.Column("cwe", sa.String(length=200), nullable=True, comment="security task: CWE id + title"),
    )

    # Backfill: a project's task is the one its selected option was ranked on. The
    # requirements profile stores the requested task types (one, in practice — the
    # recommender rejects a mixed request); take the first when it is a project task.
    op.execute(
        """
        UPDATE project AS p
        SET task_type = rp.task_types[1]
        FROM recommendation_option AS ro
        JOIN requirements_profile AS rp ON rp.id = ro.profile_id
        WHERE ro.id = p.selected_option_id
          AND rp.task_types IS NOT NULL
          AND rp.task_types[1] IN ('ci_review', 'security_analysis')
        """
    )

    # One vocabulary for the run's task: the S11 literal `code_review` IS `ci_review`.
    op.execute("UPDATE ci_run SET task = 'ci_review' WHERE task = 'code_review'")
    op.alter_column("ci_run", "task", server_default="ci_review")

    # Seeded security findings (P38c) spell their CWE at the head of the message:
    # "CWE-89: SQL injection — request parameter …". Lift "CWE-89: SQL injection" into
    # the new column so the live demo shows CWEs without a re-seed.
    op.execute(
        """
        UPDATE ci_finding
        SET cwe = left(rtrim(split_part(message, '—', 1)), 200)
        WHERE cwe IS NULL AND message ~ '^CWE-[0-9]+: '
        """
    )


def downgrade() -> None:
    op.alter_column("ci_run", "task", server_default="code_review")
    op.execute("UPDATE ci_run SET task = 'code_review' WHERE task = 'ci_review'")
    op.drop_column("ci_finding", "cwe")
    op.drop_column("ci_run", "cache_read_tokens")
    op.drop_column("project", "review_preferences")
    op.drop_column("project", "task_type")
