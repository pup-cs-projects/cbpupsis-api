"""Add the college scope used by overload-request review."""

import sqlalchemy as sa
from alembic import op

revision = "e170_program_college"
down_revision = "e167_enrollment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "programs",
        sa.Column("college", sa.String(length=100), nullable=True),
    )
    op.execute(
        "UPDATE programs SET college = 'CCIS' "
        "WHERE code IN ('BSCS', 'BSIT') AND college IS NULL"
    )


def downgrade() -> None:
    op.drop_column("programs", "college")
