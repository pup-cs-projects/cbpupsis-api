"""Store advisory, disciplinary, and other student enrollment holds."""

import sqlalchemy as sa
from alembic import op

from cbpupsis_core.config import settings

revision = "e171_student_holds"
down_revision = "e170_program_college"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "student_holds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=30), nullable=False),
        sa.Column("office", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column(
            "amount_outstanding",
            sa.Numeric(precision=10, scale=2),
            server_default="0",
            nullable=False,
        ),
        sa.Column("is_blocking", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "blocks_enrollment", sa.Boolean(), server_default="true", nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_student_holds_student_active",
        "student_holds",
        ["student_id", "resolved_at"],
    )
    role = (settings.audit_runtime_role or "").strip()
    if not role:
        raise RuntimeError("AUDIT_RUNTIME_ROLE must identify a restricted runtime role")
    quoted = op.get_bind().dialect.identifier_preparer.quote_identifier(role)
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE student_holds TO {quoted}"
    )


def downgrade() -> None:
    op.drop_index("idx_student_holds_student_active", table_name="student_holds")
    op.drop_table("student_holds")
