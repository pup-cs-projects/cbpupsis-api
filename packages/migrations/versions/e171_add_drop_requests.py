"""Add composite add/drop items and request review state.

Revision ID: e171_add_drop_requests
Revises: e170_program_college
"""

import sqlalchemy as sa
from alembic import op

revision = "e171_add_drop_requests"
down_revision = "e170_program_college"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Update add_drop_requests table
    op.add_column(
        "add_drop_requests",
        sa.Column("academic_term_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_add_drop_requests_academic_term_id",
        "add_drop_requests",
        "academic_terms",
        ["academic_term_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "idx_fk_add_drop_requests_academic_term_id",
        "add_drop_requests",
        ["academic_term_id"],
    )
    op.add_column(
        "add_drop_requests",
        sa.Column(
            "state",
            sa.String(length=20),
            server_default="PENDING",
            nullable=False,
        ),
    )
    op.create_index(
        "idx_fk_add_drop_requests_state",
        "add_drop_requests",
        ["state"],
    )
    op.add_column(
        "add_drop_requests",
        sa.Column("remarks", sa.Text(), nullable=True),
    )
    op.add_column(
        "add_drop_requests",
        sa.Column("units_before", sa.Numeric(precision=4, scale=1), nullable=True),
    )
    op.add_column(
        "add_drop_requests",
        sa.Column("units_after", sa.Numeric(precision=4, scale=1), nullable=True),
    )
    op.add_column(
        "add_drop_requests",
        sa.Column(
            "version",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
    )

    # Drop legacy single-course columns
    with op.batch_alter_table("add_drop_requests") as batch_op:
        batch_op.drop_column("reason")
        batch_op.drop_column("request_type")
        batch_op.drop_column("section_id")

    # 2. Create add_drop_request_items table
    op.create_table(
        "add_drop_request_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "request_id",
            sa.Uuid(),
            sa.ForeignKey("add_drop_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "section_id",
            sa.Uuid(),
            sa.ForeignKey("course_sections.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=10), nullable=False),
        sa.Column("reason", sa.String(length=100), nullable=True),
    )
    op.create_index(
        "idx_fk_add_drop_items_request_id",
        "add_drop_request_items",
        ["request_id"],
    )
    op.create_index(
        "idx_fk_add_drop_items_section_id",
        "add_drop_request_items",
        ["section_id"],
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        roles = bind.scalars(sa.text("""
            SELECT grantee FROM information_schema.role_table_grants
            WHERE table_schema = 'public' AND table_name = 'term_enrollments'
              AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE', 'DELETE')
              AND grantee <> 'PUBLIC'
            GROUP BY grantee HAVING count(DISTINCT privilege_type) = 4
        """))
        for role in roles:
            quoted = bind.dialect.identifier_preparer.quote_identifier(role)
            op.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE add_drop_request_items TO {quoted}"
            )


def downgrade() -> None:
    op.drop_index("idx_fk_add_drop_items_section_id", table_name="add_drop_request_items")
    op.drop_index("idx_fk_add_drop_items_request_id", table_name="add_drop_request_items")
    op.drop_table("add_drop_request_items")

    with op.batch_alter_table("add_drop_requests") as batch_op:
        batch_op.add_column(
            sa.Column(
                "section_id",
                sa.Uuid(),
                sa.ForeignKey("course_sections.id", ondelete="CASCADE"),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("request_type", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("reason", sa.String(length=100), nullable=True))

    op.drop_column("add_drop_requests", "version")
    op.drop_column("add_drop_requests", "units_after")
    op.drop_column("add_drop_requests", "units_before")
    op.drop_column("add_drop_requests", "remarks")
    op.drop_index("idx_fk_add_drop_requests_state", table_name="add_drop_requests")
    op.drop_column("add_drop_requests", "state")
    op.drop_index("idx_fk_add_drop_requests_academic_term_id", table_name="add_drop_requests")
    op.drop_constraint("fk_add_drop_requests_academic_term_id", "add_drop_requests", type_="foreignkey")
    op.drop_column("add_drop_requests", "academic_term_id")
