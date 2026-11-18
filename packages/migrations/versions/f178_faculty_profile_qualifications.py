"""Add qualifications column to faculty_profiles.

Revision ID: f178_faculty_qualifications
Revises: f177_section_grade_submissions
"""

import sqlalchemy as sa
from alembic import op

revision = "f178_faculty_qualifications"
down_revision = "f177_section_grade_submissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "faculty_profiles",
        sa.Column("qualifications", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("faculty_profiles", "qualifications")
