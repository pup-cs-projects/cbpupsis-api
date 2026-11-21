"""Add qualifications column to faculty_profiles.

Revision ID: f178_faculty_qualifications
Revises: f178_inc_lifecycle
"""

import sqlalchemy as sa
from alembic import op

revision = "f178_faculty_qualifications"
down_revision = "f178_inc_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "faculty_profiles",
        sa.Column("qualifications", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("faculty_profiles", "qualifications")
