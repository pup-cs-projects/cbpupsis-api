"""Join published correction and faculty-profile migration histories.

Revision ID: m103_corrections_profile
Revises: f103_grade_corrections, f178_faculty_qualifications
"""

revision = "m103_corrections_profile"
down_revision = ("f103_grade_corrections", "f178_faculty_qualifications")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
