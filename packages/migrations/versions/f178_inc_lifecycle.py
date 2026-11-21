"""Add inc_expiry_runs table and pending-completion uniqueness guard.

Revision ID: f178_inc_lifecycle
Revises: f177_section_grade_submissions
"""

import sqlalchemy as sa
from alembic import op

revision = "f178_inc_lifecycle"
down_revision = "f177_section_grade_submissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inc_expiry_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_key", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="RUNNING",
            nullable=False,
        ),
        sa.Column(
            "processed_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "converted_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("run_key", name="uq_inc_expiry_runs_run_key"),
    )

    op.create_index(
        "uq_incomplete_completions_pending_grade",
        "incomplete_completions",
        ["grade_id"],
        unique=True,
        postgresql_where=sa.text("state = 'PENDING_DEAN'"),
        sqlite_where=sa.text("state = 'PENDING_DEAN'"),
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
                f"ON TABLE inc_expiry_runs TO {quoted}"
            )


def downgrade() -> None:
    op.drop_index(
        "uq_incomplete_completions_pending_grade",
        table_name="incomplete_completions",
    )
    op.drop_table("inc_expiry_runs")
