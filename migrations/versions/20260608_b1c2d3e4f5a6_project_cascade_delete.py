"""project delete cascade (S15d): ON DELETE CASCADE on the project subtree

S15d adds `DELETE /projects/{id}`. A project owns a subtree — its Jenkins
connection, its CI runs (each with findings → feedback, plus llm_call rows), its
chat messages (→ retrieval traces) and its proactive alerts. The initial schema
declared all those FKs as the PG default NO ACTION, so a naive `DELETE FROM project`
raises a foreign-key violation. Rather than ordering deletes by hand in the service
(which silently breaks the day a new child table is added), make the DATABASE the
backstop: recreate each child FK with `ON DELETE CASCADE` so one delete removes the
whole subtree atomically. This matches the project's S13/S14b "DB enforces the
invariant" posture.

Scope is exactly the project subtree — the FKs the project (or its descendants)
*owns*. The project's own *parent* references (selected_option_id → recommendation
_option, baseline_model_id → model) are left untouched: deleting a project must not
delete the catalog/recommendation rows it merely points at.

Pure DDL (drop + recreate each FK). No data migration. compare_metadata does not
diff FK ondelete, so the autogenerate-no-drift parity test stays green; the ORM is
updated to carry the same ondelete for documentation parity.

Revision ID: b1c2d3e4f5a6
Revises: a1b2c3d4e5f6
Create Date: 2026-06-08 16:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (constraint, source_table, [source_col], referent_table, [referent_col]) — the
# eight PG-default-named FKs that make up the project subtree, in no particular
# order (each drop+recreate is independent).
_FKS = [
    ("jenkins_connection_project_id_fkey", "jenkins_connection", "project_id", "project", "id"),
    ("ci_run_project_id_fkey", "ci_run", "project_id", "project", "id"),
    ("ci_finding_ci_run_id_fkey", "ci_finding", "ci_run_id", "ci_run", "id"),
    ("finding_feedback_ci_finding_id_fkey", "finding_feedback", "ci_finding_id", "ci_finding", "id"),
    ("llm_call_ci_run_id_fkey", "llm_call", "ci_run_id", "ci_run", "id"),
    ("chat_message_project_id_fkey", "chat_message", "project_id", "project", "id"),
    ("retrieval_trace_chat_message_id_fkey", "retrieval_trace", "chat_message_id", "chat_message", "id"),
    ("proactive_alert_project_id_fkey", "proactive_alert", "project_id", "project", "id"),
]


def upgrade() -> None:
    for name, src, src_col, ref, ref_col in _FKS:
        op.drop_constraint(name, src, type_="foreignkey")
        op.create_foreign_key(name, src, ref, [src_col], [ref_col], ondelete="CASCADE")


def downgrade() -> None:
    for name, src, src_col, ref, ref_col in _FKS:
        op.drop_constraint(name, src, type_="foreignkey")
        op.create_foreign_key(name, src, ref, [src_col], [ref_col])
