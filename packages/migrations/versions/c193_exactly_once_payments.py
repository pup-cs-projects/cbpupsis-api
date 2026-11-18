"""Store exactly-once Xendit attempts, deliveries, and reconciliation runs.

Revision ID: c193_exactly_once_payments
Revises: c192_student_account_late_fine
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c193_exactly_once_payments"
down_revision = "c192_student_account_late_fine"
branch_labels = None
depends_on = None


def _runtime_roles(bind):
    return bind.scalars(
        sa.text("""
        SELECT grantee FROM information_schema.role_table_grants
        WHERE table_schema = 'public' AND table_name = 'payments'
          AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE', 'DELETE')
          AND grantee <> 'PUBLIC'
        GROUP BY grantee HAVING count(DISTINCT privilege_type) = 4
    """)
    )


def upgrade() -> None:
    op.add_column("payments", sa.Column("student_id", sa.Uuid(), nullable=True))
    op.add_column("payments", sa.Column("idempotency_key", sa.String(255)))
    op.add_column(
        "payments",
        sa.Column(
            "idempotency_status",
            sa.String(20),
            server_default="COMPLETED",
            nullable=False,
        ),
    )
    op.add_column("payments", sa.Column("merchant_reference", sa.String(64)))
    op.add_column("payments", sa.Column("gateway_session_id", sa.String(150)))
    op.add_column("payments", sa.Column("gateway_checkout_url", sa.Text()))
    op.add_column("payments", sa.Column("response_status", sa.SmallInteger()))
    op.add_column("payments", sa.Column("response_body", postgresql.JSONB()))
    op.add_column("payments", sa.Column("initiated_ip", postgresql.INET()))
    op.add_column(
        "payments",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        "fk_payments_student_id_users",
        "payments",
        "users",
        ["student_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.execute("""
        UPDATE payments p SET
          student_id = a.student_id,
          idempotency_key = 'legacy-' || p.id::text,
          merchant_reference = 'legacy-' || p.id::text
        FROM student_billing_accounts a
        WHERE a.id = p.billing_account_id
    """)
    op.alter_column("payments", "student_id", nullable=False)
    op.alter_column("payments", "idempotency_key", nullable=False)
    op.alter_column("payments", "merchant_reference", nullable=False)
    op.alter_column("payments", "gateway_reference", nullable=True)
    op.alter_column("payments", "payment_method", server_default="xendit")
    op.alter_column("payments", "payment_status", server_default="PENDING")
    op.alter_column("payments", "idempotency_status", server_default="PROCESSING")
    op.create_unique_constraint(
        "uq_payments_student_idempotency",
        "payments",
        ["student_id", "idempotency_key"],
    )
    op.create_unique_constraint(
        "uq_payments_merchant_reference", "payments", ["merchant_reference"]
    )
    op.create_unique_constraint(
        "uq_payments_gateway_session_id", "payments", ["gateway_session_id"]
    )
    op.create_index(
        "idx_payments_pending_created", "payments", ["payment_status", "created_at"]
    )

    op.create_table(
        "payment_webhook_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("webhook_id", sa.String(150), nullable=False, unique=True),
        sa.Column(
            "payment_id",
            sa.Uuid(),
            sa.ForeignKey("payments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("event_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_payment_webhook_events_payment_id",
        "payment_webhook_events",
        ["payment_id"],
    )
    op.create_table(
        "payment_reconciliation_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_key", sa.String(255), nullable=False, unique=True),
        sa.Column("status", sa.String(20), server_default="RUNNING", nullable=False),
        sa.Column("processed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("settled_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )

    bind = op.get_bind()
    for role in _runtime_roles(bind):
        quoted = bind.dialect.identifier_preparer.quote_identifier(role)
        op.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
            f"payment_webhook_events, payment_reconciliation_runs TO {quoted}"
        )


def downgrade() -> None:
    op.drop_table("payment_reconciliation_runs")
    op.drop_index(
        "ix_payment_webhook_events_payment_id", table_name="payment_webhook_events"
    )
    op.drop_table("payment_webhook_events")
    op.drop_index("idx_payments_pending_created", table_name="payments")
    op.drop_constraint("uq_payments_gateway_session_id", "payments", type_="unique")
    op.drop_constraint("uq_payments_merchant_reference", "payments", type_="unique")
    op.drop_constraint("uq_payments_student_idempotency", "payments", type_="unique")
    op.execute("""
        UPDATE payments
        SET gateway_reference = merchant_reference
        WHERE gateway_reference IS NULL
    """)
    op.alter_column("payments", "gateway_reference", nullable=False)
    op.alter_column("payments", "payment_method", server_default=None)
    op.alter_column("payments", "payment_status", server_default=None)
    op.drop_constraint("fk_payments_student_id_users", "payments", type_="foreignkey")
    for column in (
        "updated_at",
        "initiated_ip",
        "response_body",
        "response_status",
        "gateway_checkout_url",
        "gateway_session_id",
        "merchant_reference",
        "idempotency_status",
        "idempotency_key",
        "student_id",
    ):
        op.drop_column("payments", column)
