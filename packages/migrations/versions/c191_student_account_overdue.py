"""Persist the student account's transactional overdue-notification marker.

Revision ID: c191_student_account_overdue
Revises: c190_fee_structures_term_config
"""

import sqlalchemy as sa
from alembic import op

revision = "c191_student_account_overdue"
down_revision = "c190_fee_structures_term_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "student_billing_accounts",
        sa.Column("overdue_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    # A downgrade/re-upgrade must not re-notify accounts whose durable outbox
    # event survived the schema rollback. No financial state is reconstructed.
    op.execute("""
        UPDATE student_billing_accounts AS account
        SET overdue_notified_at = (
            SELECT min(message.created_at) FROM outbox_messages AS message
            WHERE message.event_name = 'billing.account_overdue'
              AND message.payload->>'billing_account_id' = account.id::text
              AND message.payload->>'user_id' = account.student_id::text
              AND message.payload->>'term_id' = account.academic_term_id::text
        )
    """)


def downgrade() -> None:
    op.drop_column("student_billing_accounts", "overdue_notified_at")
