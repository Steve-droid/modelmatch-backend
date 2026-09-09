"""Google identity and single-use login challenges (P38n)."""
from alembic import op
import sqlalchemy as sa

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("user", "password_hash", existing_type=sa.String(255), nullable=True)
    op.add_column("user", sa.Column("google_subject", sa.String(255), nullable=True))
    op.create_unique_constraint("user_google_subject_key", "user", ["google_subject"])
    op.create_table(
        "google_login_nonce",
        sa.Column("nonce", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_google_login_nonce_expires_at", "google_login_nonce", ["expires_at"])


def downgrade():
    # Preserve Google-only users and their projects. An invalid hash keeps password
    # login disabled on the older app; the recognized prefix reaches argon2
    # verification and returns its handled VerificationError, never a 500.
    op.execute("UPDATE \"user\" SET password_hash = '$argon2id$disabled' WHERE password_hash IS NULL")
    op.drop_table("google_login_nonce")
    op.drop_constraint("user_google_subject_key", "user", type_="unique")
    op.drop_column("user", "google_subject")
    op.alter_column("user", "password_hash", existing_type=sa.String(255), nullable=False)
