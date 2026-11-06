"""Add catalog scope columns, curriculum effective admission year, and substitutions.

Revision ID: c187_admin_catalog
Revises: e170_program_college
"""

import sqlalchemy as sa
from alembic import op

revision = "c187_admin_catalog"
down_revision = "e170_program_college"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("courses", sa.Column("department", sa.String(100), nullable=True))
    op.add_column("courses", sa.Column("college", sa.String(100), nullable=True))
    op.add_column(
        "curricula",
        sa.Column("effective_admission_year", sa.SmallInteger(), nullable=True),
    )
    op.create_table(
        "course_substitutions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "student_id",
            sa.Uuid(),
            sa.ForeignKey("student_profiles.user_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "original_course_id",
            sa.Uuid(),
            sa.ForeignKey("courses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "substitute_course_id",
            sa.Uuid(),
            sa.ForeignKey("courses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "approved_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "student_id",
            "original_course_id",
            name="uq_course_substitutions_student_orig",
        ),
    )
    bind = op.get_bind()
    roles = bind.scalars(
        sa.text("""
        SELECT grantee FROM information_schema.role_table_grants
        WHERE table_schema = 'public' AND table_name = 'courses'
          AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE', 'DELETE')
          AND grantee <> 'PUBLIC'
        GROUP BY grantee HAVING count(DISTINCT privilege_type) = 4
    """)
    )
    for role in roles:
        quoted = bind.dialect.identifier_preparer.quote_identifier(role)
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE course_substitutions TO {quoted}"
        )


def downgrade() -> None:
    op.drop_table("course_substitutions")
    op.drop_column("curricula", "effective_admission_year")
    op.drop_column("courses", "college")
    op.drop_column("courses", "department")
