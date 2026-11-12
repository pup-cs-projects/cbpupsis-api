"""Store typed administrative and disciplinary student holds.

Revision ID: c188_student_holds
Revises: c187_admin_catalog
"""

import sqlalchemy as sa
from alembic import op

revision = "c188_student_holds"
down_revision = "c187_admin_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "student_holds",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "student_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("hold_type", sa.String(30), nullable=False),
        sa.Column("office", sa.String(150), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("amount_outstanding", sa.Numeric(10, 2), nullable=True),
        sa.Column("currency", sa.String(3), server_default="PHP", nullable=False),
        sa.Column(
            "blocks_enrollment",
            sa.Boolean(),
            server_default="true",
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "idx_student_holds_student_unresolved",
        "student_holds",
        ["student_id", "resolved_at"],
    )
    bind = op.get_bind()
    roles = bind.scalars(
        sa.text("""
        SELECT grantee FROM information_schema.role_table_grants
        WHERE table_schema = 'public'
          AND table_name = 'student_billing_accounts'
          AND privilege_type = 'SELECT'
          AND grantee <> 'PUBLIC'
    """)
    )
    for role in roles:
        quoted = bind.dialect.identifier_preparer.quote_identifier(role)
        op.execute(f"GRANT SELECT ON TABLE student_holds TO {quoted}")


def downgrade() -> None:
    op.drop_index("idx_student_holds_student_unresolved", table_name="student_holds")
    op.drop_table("student_holds")
