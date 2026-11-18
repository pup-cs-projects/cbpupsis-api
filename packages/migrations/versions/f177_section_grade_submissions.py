"""Add section_grade_submissions table for draft/finalize state and optimistic locking.

Revision ID: f177_section_grade_submissions
Revises: c189_student_holds_acl
"""

import sqlalchemy as sa
from alembic import op

revision = "f177_section_grade_submissions"
down_revision = "c189_student_holds_acl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "section_grade_submissions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "section_id",
            sa.Uuid(),
            sa.ForeignKey("course_sections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.String(length=20),
            server_default="DRAFT",
            nullable=False,
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column(
            "finalized_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "section_id", name="uq_section_grade_submissions_section_id"
        ),
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        roles = bind.scalars(
            sa.text("""
            SELECT grantee FROM information_schema.role_table_grants
            WHERE table_schema = 'public'
              AND table_name = 'student_grades'
              AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE', 'DELETE')
              AND grantee <> 'PUBLIC'
            GROUP BY grantee HAVING count(DISTINCT privilege_type) = 4
        """)
        )
        for role in roles:
            quoted = bind.dialect.identifier_preparer.quote_identifier(role)
            op.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE "
                f"ON TABLE section_grade_submissions TO {quoted}"
            )


def downgrade() -> None:
    op.drop_table("section_grade_submissions")
