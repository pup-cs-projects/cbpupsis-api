"""Persist one approved flat fine without changing base assessments or CORs.

Revision ID: c192_student_account_late_fine
Revises: c191_student_account_overdue
"""

import sqlalchemy as sa
from alembic import op

revision = "c192_student_account_late_fine"
down_revision = "c191_student_account_overdue"
branch_labels = None
depends_on = None


def _replace_balances(include_penalty: bool) -> None:
    op.drop_column("student_billing_accounts", "balance")
    op.drop_column("student_billing_accounts", "net_amount_due")
    net = "gross_amount - fhe_subsidy_amount"
    if include_penalty:
        net = "gross_amount + late_penalty_amount - fhe_subsidy_amount"
    op.add_column("student_billing_accounts", sa.Column(
        "net_amount_due", sa.Numeric(10, 2), sa.Computed(net, persisted=True),
    ))
    op.add_column("student_billing_accounts", sa.Column(
        "balance", sa.Numeric(10, 2), sa.Computed(f"{net} - amount_paid", persisted=True),
    ))


def upgrade() -> None:
    op.add_column("student_billing_accounts", sa.Column(
        "late_penalty_amount", sa.Numeric(10, 2), nullable=False, server_default="0",
    ))
    op.add_column("student_billing_accounts", sa.Column(
        "late_penalty_applied_at", sa.DateTime(timezone=True), nullable=True,
    ))
    op.create_check_constraint(
        "ck_billing_late_penalty_state", "student_billing_accounts",
        "(late_penalty_applied_at IS NULL AND late_penalty_amount = 0) OR "
        "(late_penalty_applied_at IS NOT NULL AND late_penalty_amount > 0)",
    )
    _replace_balances(True)


def downgrade() -> None:
    # Dropping an unpaid or paid fine would destroy financial history. An owner
    # must explicitly reconcile it before this schema can safely be rolled back.
    # Serialize the check and DDL with writers so a fine cannot appear between them.
    op.execute("LOCK TABLE student_billing_accounts IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text("""
        SELECT EXISTS (SELECT 1 FROM student_billing_accounts
        WHERE late_penalty_amount <> 0 OR late_penalty_applied_at IS NOT NULL)
    """)):
        raise RuntimeError("Recorded late fines require explicit reconciliation before downgrade")
    _replace_balances(False)
    op.drop_constraint("ck_billing_late_penalty_state", "student_billing_accounts", type_="check")
    op.drop_column("student_billing_accounts", "late_penalty_applied_at")
    op.drop_column("student_billing_accounts", "late_penalty_amount")
