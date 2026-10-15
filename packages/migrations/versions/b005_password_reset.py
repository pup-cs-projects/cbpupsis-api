"""Password reset session generations and shared address throttling.

Revision ID: b005reset
Revises: 99a37a906c0d
"""

import sqlalchemy as sa
from alembic import op

revision = "b005reset"
down_revision = "99a37a906c0d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("session_version", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_table(
        "password_reset_throttles",
        sa.Column("email_hash", sa.String(64), primary_key=True),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("password_reset_throttles")
    op.drop_column("users", "session_version")
