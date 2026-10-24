"""Persist student calendar revisions and cancellation tombstones.

Revision ID: d096_schedule_export
Revises: 180508baf0e2
"""

import sqlalchemy as sa
from alembic import op

revision = "d096_schedule_export"
down_revision = "180508baf0e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schedule_export_states",
        sa.Column("student_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("academic_term_id", sa.Uuid(), sa.ForeignKey("academic_terms.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False, server_default=""),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("events", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # The existing ACL migration deliberately defines no blanket default grants.
    # Mirror only named roles already trusted to mutate ordinary enrollment data.
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
        op.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE "
            f"ON TABLE schedule_export_states TO {quoted}"
        )


def downgrade() -> None:
    op.drop_table("schedule_export_states")
