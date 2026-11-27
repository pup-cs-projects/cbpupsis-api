"""Persist private correction evidence and scope; permit undefined cached GWA.

Revision ID: f103_grade_corrections
Revises: f178_inc_lifecycle
"""

import sqlalchemy as sa
from alembic import op

revision = "f103_grade_corrections"
down_revision = "f178_inc_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("grade_corrections", sa.Column("section_id", sa.Uuid()))
    op.add_column("grade_corrections", sa.Column("student_id", sa.Uuid()))
    # Relational identity is recoverable; historical file bytes, source versions,
    # release decisions and audit evidence must never be fabricated.
    op.execute("""
        UPDATE grade_corrections AS c
        SET section_id = e.section_id, student_id = t.student_id
        FROM student_grades g
        JOIN student_course_enrollments e ON e.id = g.enrollment_item_id
        JOIN term_enrollments t ON t.id = e.term_enrollment_id
        WHERE c.grade_id = g.id
    """)
    op.alter_column("grade_corrections", "section_id", nullable=False)
    op.alter_column("grade_corrections", "student_id", nullable=False)
    op.create_foreign_key(
        "fk_correction_section",
        "grade_corrections",
        "course_sections",
        ["section_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_correction_student",
        "grade_corrections",
        "users",
        ["student_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_grade_corrections_section_id", "grade_corrections", ["section_id"]
    )
    op.create_index(
        "ix_grade_corrections_student_id", "grade_corrections", ["student_id"]
    )
    for column in (
        sa.Column("grading_sheet_content", sa.LargeBinary()),
        sa.Column("grading_sheet_content_type", sa.String(32)),
        sa.Column("grading_sheet_size", sa.Integer()),
        sa.Column("grading_sheet_sha256", sa.String(64)),
        sa.Column("window_closed_at", sa.DateTime(timezone=True)),
        sa.Column("grade_updated_at", sa.DateTime(timezone=True)),
        sa.Column("rejected_by", sa.Uuid()),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("grade_corrections", column)
    op.create_foreign_key(
        "fk_correction_rejector",
        "grade_corrections",
        "users",
        ["rejected_by"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.alter_column("student_term_gpa", "term_gpa", nullable=True)
    op.alter_column("student_term_gpa", "cumulative_gpa", nullable=True)


def downgrade() -> None:
    # Refuse rather than silently replace an undefined average with a fake zero.
    op.alter_column("student_term_gpa", "term_gpa", nullable=False)
    op.alter_column("student_term_gpa", "cumulative_gpa", nullable=False)
    op.drop_constraint(
        "fk_correction_rejector", "grade_corrections", type_="foreignkey"
    )
    op.drop_constraint("fk_correction_section", "grade_corrections", type_="foreignkey")
    op.drop_constraint("fk_correction_student", "grade_corrections", type_="foreignkey")
    op.drop_index("ix_grade_corrections_section_id", table_name="grade_corrections")
    op.drop_index("ix_grade_corrections_student_id", table_name="grade_corrections")
    for name in (
        "section_id",
        "student_id",
        "grading_sheet_content",
        "grading_sheet_content_type",
        "grading_sheet_size",
        "grading_sheet_sha256",
        "window_closed_at",
        "grade_updated_at",
        "rejected_by",
        "resolved_at",
    ):
        op.drop_column("grade_corrections", name)
