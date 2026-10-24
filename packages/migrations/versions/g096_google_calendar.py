"""Private Google calendar connections and single-use authorization state.

Revision ID: g096_google_calendar
Revises: d096_schedule_export
"""

import sqlalchemy as sa
from alembic import op

revision = "g096_google_calendar"
down_revision = "d096_schedule_export"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "google_calendar_accounts",
        sa.Column("student_id", sa.Uuid(), primary_key=True),
        sa.Column("generation", sa.Uuid(), nullable=False),
        sa.Column("subject_hash", sa.String(64)),
        sa.Column("refresh_cipher", sa.Text()),
        sa.Column("revoke_cipher", sa.Text()),
        sa.Column("targets", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("next_sync_at", sa.DateTime(timezone=True)),
        sa.Column("last_sync_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(64)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_id", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_google_calendar_accounts_next_sync_at", "google_calendar_accounts", ["next_sync_at"])
    op.create_table(
        "google_calendar_oauth_states",
        sa.Column("student_id", sa.Uuid(), primary_key=True),
        sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("payload_cipher", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    bind = op.get_bind()
    roles = bind.scalars(sa.text("""
        SELECT grantee FROM information_schema.role_table_grants
        WHERE table_schema = 'public' AND table_name = 'term_enrollments'
          AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE', 'DELETE')
          AND grantee <> 'PUBLIC'
        GROUP BY grantee HAVING count(DISTINCT privilege_type) = 4
    """))
    for role in roles:
        quoted = bind.dialect.identifier_preparer.quote_identifier(role)
        op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
                   f"google_calendar_accounts, google_calendar_oauth_states TO {quoted}")


def downgrade() -> None:
    op.drop_table("google_calendar_oauth_states")
    op.drop_index("ix_google_calendar_accounts_next_sync_at", table_name="google_calendar_accounts")
    op.drop_table("google_calendar_accounts")
