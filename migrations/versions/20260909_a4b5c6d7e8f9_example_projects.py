"""Explicit sample-project identity; existing projects remain real."""
from alembic import op
import sqlalchemy as sa

revision = "a4b5c6d7e8f9"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("project", sa.Column("is_example", sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
    op.create_index("uq_project_user_example_task", "project", ["user_id", "task_type"],
                    unique=True, postgresql_where=sa.text("is_example"))


def downgrade():
    op.drop_index("uq_project_user_example_task", table_name="project")
    op.drop_column("project", "is_example")
