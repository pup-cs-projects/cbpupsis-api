"""Add term configuration columns and nullable program_id to fee_structures.

Revision ID: c190_fee_structures_term_config
Revises: c189_student_holds_acl
"""

import sqlalchemy as sa
from alembic import op

revision = "c190_fee_structures_term_config"
down_revision = "c189_student_holds_acl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("fee_structures", "program_id", nullable=True)
    op.add_column(
        "fee_structures",
        sa.Column("due_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "fee_structures",
        sa.Column("subsidy_covered_fees", sa.JSON(), nullable=True),
    )
    op.add_column(
        "fee_structures",
        sa.Column("installments", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("fee_structures", "installments")
    op.drop_column("fee_structures", "subsidy_covered_fees")
    op.drop_column("fee_structures", "due_date")
    op.alter_column("fee_structures", "program_id", nullable=False)
