"""Store profile pictures in the database so every app instance can serve them.

Revision ID: a098_user_avatars
Revises: m097_merge_pii_calendar
"""

import sqlalchemy as sa
from alembic import op

revision = "a098_user_avatars"
down_revision = "m097_merge_pii_calendar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_avatars",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("content_type", sa.String(32), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # The ACL migration defines no blanket default grants. Mirror only the named
    # roles already trusted to write the profile rows these pictures belong to.
    bind = op.get_bind()
    roles = bind.scalars(sa.text("""
        SELECT grantee FROM information_schema.role_table_grants
        WHERE table_schema = 'public' AND table_name = 'user_profiles'
          AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE', 'DELETE')
          AND grantee <> 'PUBLIC'
        GROUP BY grantee HAVING count(DISTINCT privilege_type) = 4
    """))
    for role in roles:
        quoted = bind.dialect.identifier_preparer.quote_identifier(role)
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE user_avatars TO {quoted}"
        )
    # Avatar URLs written before this table existed point at bytes that only
    # lived in a process's memory; clear them so the portal shows its placeholder.
    op.execute(
        "UPDATE user_profiles SET avatar_url = NULL "
        "WHERE avatar_url LIKE '/api/v1/student/profile/avatar/%'"
    )


def downgrade() -> None:
    op.drop_table("user_avatars")
