"""Data access for the billing domain.

Only this module builds SQL or calls into the session for billing models.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.billing import StudentBillingAccount


async def get_student_billing_account(
    db: AsyncSession, student_id: uuid.UUID, term_id: uuid.UUID
) -> StudentBillingAccount | None:
    """Return student billing account for term.

    SQL::

        SELECT student_billing_accounts.id,
               student_billing_accounts.student_id,
               student_billing_accounts.academic_term_id,
               student_billing_accounts.gross_amount,
               student_billing_accounts.fhe_subsidy_amount,
               student_billing_accounts.net_amount_due,
               student_billing_accounts.amount_paid,
               student_billing_accounts.balance,
               student_billing_accounts.payment_status,
               student_billing_accounts.due_date
        FROM student_billing_accounts
        WHERE student_billing_accounts.student_id = :student_id
          AND student_billing_accounts.academic_term_id = :term_id
        LIMIT 1
    """
    stmt = (
        select(StudentBillingAccount)
        .where(
            StudentBillingAccount.student_id == student_id,
            StudentBillingAccount.academic_term_id == term_id,
        )
        .limit(1)
    )
    return await db.scalar(stmt)


async def get_total_outstanding_balance(
    db: AsyncSession, student_id: uuid.UUID
) -> Decimal:
    """Return total outstanding balance across unpaid billing accounts.

    SQL::

        SELECT COALESCE(SUM(student_billing_accounts.balance), 0)
        FROM student_billing_accounts
        WHERE student_billing_accounts.student_id = :student_id
          AND student_billing_accounts.payment_status != 'paid'
    """
    stmt = select(func.coalesce(func.sum(StudentBillingAccount.balance), 0)).where(
        StudentBillingAccount.student_id == student_id,
        StudentBillingAccount.payment_status != "paid",
    )
    res = await db.scalar(stmt)
    return Decimal(str(res)) if res is not None else Decimal("0.00")
