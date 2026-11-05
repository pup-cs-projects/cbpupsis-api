"""Data access for the billing domain.

Only this module builds SQL or calls into the session for billing models.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.billing import FeeStructure, StudentBillingAccount


async def get_fee_structure(db, program_id, term_id):
    """Read the configured fee schedule, never inventing fallback fees.

    SQL::

        SELECT * FROM fee_structures WHERE program_id=:p AND academic_term_id=:t;
    """
    return await db.scalar(
        select(FeeStructure).where(
            FeeStructure.program_id == program_id,
            FeeStructure.academic_term_id == term_id,
        )
    )


async def store_assessment(db, stored, due_date: date):
    """Stage inputs; caller already owns the parent enrollment row lock.

    SQL::

        INSERT INTO student_billing_accounts (student_id, academic_term_id,
            gross_amount, fhe_subsidy_amount, assessment_snapshot, due_date)
        VALUES (:student, :term, :gross, :subsidy, :snapshot, :due);
        -- Existing accounts instead update assessment fields, preserving payments:
        UPDATE student_billing_accounts SET gross_amount=:gross,
            fhe_subsidy_amount=:subsidy, assessment_snapshot=:snapshot WHERE id=:id;
    """
    row = await get_student_billing_account(
        db, stored.inputs.student_id, stored.inputs.term_id
    )
    if row is None:
        row = StudentBillingAccount(
            student_id=stored.inputs.student_id,
            academic_term_id=stored.inputs.term_id,
            due_date=due_date,
        )
        db.add(row)
    row.gross_amount = Decimal(stored.assessment.amountAssessed)
    row.fhe_subsidy_amount = Decimal(stored.assessment.subsidyApplied)
    row.assessment_snapshot = stored.model_dump(mode="json")
    await db.flush()


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
