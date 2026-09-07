"""benchmark as_of + notes (P38c: dated benchmark snapshots)

A benchmark is a moving target. CodeReviewBench's public leaderboard changed BOTH
its metric and its model set after our figures were taken (it now publishes
light-v1 F1/recall/precision), so our `review_score_percent` rows are a **June 2026
snapshot** and must say so wherever they are shown — otherwise the pick screen
implies a live reading of a leaderboard that no longer reports that metric.

Two ways to carry that were considered. Renaming the benchmark to
"CodeReviewBench (Jun 2026 snapshot)" needs no migration, but the name is the
benchmark's natural key: renaming it orphans every row already in a live database
and bakes a date into a join key that a future refresh job would have to churn.
So instead the date becomes DATA on the benchmark row:

  * `as_of`  — the date the figures in this benchmark's rows were taken, and
  * `notes`  — why that date matters (what changed since, what the metric means).

Both are nullable: benchmarks that are continuously refreshed simply leave them
empty. The curated `chat_catalog` view gains both columns (appended at the end, so
CREATE OR REPLACE VIEW keeps the read-only role's existing grant) — the grounded
chat can then answer "how old is this score?" from the catalog instead of guessing.

Revision ID: a7b8c9d0e1f2
Revises: e6f7a8b9c0d1
Create Date: 2026-09-07 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same view as the S14b migration, plus the two benchmark-provenance columns. New
# columns are APPENDED so this is a valid CREATE OR REPLACE (Postgres allows adding
# columns at the end of a view, not in the middle) — which preserves the grant held
# by the read-only chat role instead of dropping and re-granting it.
_VIEW = """
CREATE OR REPLACE VIEW chat_catalog AS
SELECT
    br.id                   AS id,
    m.name                  AS model,
    m.vendor                AS vendor,
    b.name                  AS benchmark,
    h.name                  AS harness,
    h.vendor                AS harness_vendor,
    br.task_type            AS task_type,
    br.metric               AS metric,
    br.score                AS score,
    br.cost_per_mtok        AS cost_per_mtok,
    m.input_price_per_mtok  AS input_price_per_mtok,
    m.output_price_per_mtok AS output_price_per_mtok,
    br.context_window       AS context_window,
    br.source               AS source,
    br.measured_at          AS measured_at,
    b.as_of                 AS benchmark_as_of,
    b.notes                 AS benchmark_notes
FROM benchmark_result br
JOIN model m      ON m.id = br.model_id
JOIN benchmark b  ON b.id = br.benchmark_id
LEFT JOIN harness h ON h.id = br.harness_id
"""

_VIEW_WITHOUT_BENCHMARK_PROVENANCE = """
CREATE OR REPLACE VIEW chat_catalog AS
SELECT
    br.id                   AS id,
    m.name                  AS model,
    m.vendor                AS vendor,
    b.name                  AS benchmark,
    h.name                  AS harness,
    h.vendor                AS harness_vendor,
    br.task_type            AS task_type,
    br.metric               AS metric,
    br.score                AS score,
    br.cost_per_mtok        AS cost_per_mtok,
    m.input_price_per_mtok  AS input_price_per_mtok,
    m.output_price_per_mtok AS output_price_per_mtok,
    br.context_window       AS context_window,
    br.source               AS source,
    br.measured_at          AS measured_at
FROM benchmark_result br
JOIN model m      ON m.id = br.model_id
JOIN benchmark b  ON b.id = br.benchmark_id
LEFT JOIN harness h ON h.id = br.harness_id
"""


def upgrade() -> None:
    op.add_column("benchmark", sa.Column("as_of", sa.Date(), nullable=True))
    op.add_column("benchmark", sa.Column("notes", sa.String(length=1024), nullable=True))
    op.execute(_VIEW)


def downgrade() -> None:
    # Drop the view's dependency on the columns BEFORE dropping the columns: a view
    # column cannot be removed by CREATE OR REPLACE, so the view is dropped and
    # rebuilt in its pre-P38c shape, then re-granted to the read-only chat role.
    op.execute("DROP VIEW IF EXISTS chat_catalog;")
    op.execute(_VIEW_WITHOUT_BENCHMARK_PROVENANCE)
    from app.config import get_settings

    role = get_settings().chat_readonly_db_user
    op.execute(f'GRANT SELECT ON chat_catalog TO "{role}";')
    op.drop_column("benchmark", "notes")
    op.drop_column("benchmark", "as_of")
