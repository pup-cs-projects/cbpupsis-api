import sqlalchemy as sa
from alembic import op

revision = "e167_enrollment"
down_revision = "a098_user_avatars"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "student_profiles",
        sa.Column(
            "admission_status",
            sa.String(length=30),
            server_default="REGULAR",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("student_profiles", "admission_status")
