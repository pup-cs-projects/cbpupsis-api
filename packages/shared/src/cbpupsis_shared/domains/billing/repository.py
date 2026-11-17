"""Data access for the billing domain.

Only this module builds SQL or calls into the session for billing models.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.billing import (
    FeeStructure,
    Payment,
    StudentBillingAccount,
    StudentHold,
)
from cbpupsis_shared.domains.billing.constants import MAX_ACCOUNT_AMOUNT
from cbpupsis_shared.domains.billing.exceptions import BillingAccountUnavailableError


async def list_unresolved_student_holds(
    db: AsyncSession, student_id: uuid.UUID
) -> list[StudentHold]:
    """Return unresolved holds in a stable refusal order.

    SQL::

        SELECT * FROM student_holds
        WHERE student_id = :student_id AND resolved_at IS NULL
        ORDER BY created_at, id;
    """
    return list(
        await db.scalars(
            select(StudentHold)
            .where(
                StudentHold.student_id == student_id,
                StudentHold.resolved_at.is_(None),
            )
            .order_by(StudentHold.created_at, StudentHold.id)
        )
    )


async def get_fee_structure(
    db: AsyncSession, program_id: uuid.UUID | None, term_id: uuid.UUID
) -> FeeStructure | None:
    """Read the configured fee schedule, never inventing fallback fees.

    SQL::

        SELECT * FROM fee_structures WHERE program_id=:p AND academic_term_id=:t;
    """
    res = await db.scalar(
        select(FeeStructure).where(
            FeeStructure.program_id == program_id,
            FeeStructure.academic_term_id == term_id,
        )
    )
    if res is not None:
        return res
    if program_id is not None:
        return await db.scalar(
            select(FeeStructure).where(
                FeeStructure.program_id.is_(None),
                FeeStructure.academic_term_id == term_id,
            )
        )
    return None


async def get_fee_structures_for_term(
    db: AsyncSession, term_id: uuid.UUID
) -> list[FeeStructure]:
    """Return all fee structures configured for an academic term.

    SQL::

        SELECT * FROM fee_structures
        WHERE academic_term_id = :term_id
        ORDER BY program_id NULLS FIRST;
    """
    return list(
        await db.scalars(
            select(FeeStructure)
            .where(FeeStructure.academic_term_id == term_id)
            .order_by(FeeStructure.program_id.nulls_first())
        )
    )


async def count_term_assessments(db: AsyncSession, term_id: uuid.UUID) -> int:
    """Return count of student assessments generated for this term.

    SQL::

        SELECT count(id) FROM student_billing_accounts
        WHERE academic_term_id = :term_id AND assessment_snapshot IS NOT NULL;
    """
    stmt = select(func.count(StudentBillingAccount.id)).where(
        StudentBillingAccount.academic_term_id == term_id,
        StudentBillingAccount.assessment_snapshot.is_not(None),
    )
    res = await db.scalar(stmt)
    return int(res or 0)


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
    gross = Decimal(stored.assessment.amountAssessed)
    subsidy = Decimal(stored.assessment.subsidyApplied)
    penalty = row.late_penalty_amount if row is not None else Decimal(0)
    # Reconciliation must not overflow generated totals when a fine already exists.
    if gross - subsidy + penalty > MAX_ACCOUNT_AMOUNT:
        raise BillingAccountUnavailableError
    if row is None:
        row = StudentBillingAccount(
            student_id=stored.inputs.student_id,
            academic_term_id=stored.inputs.term_id,
            due_date=due_date,
        )
        db.add(row)
    row.gross_amount = gross
    row.fhe_subsidy_amount = subsidy
    row.assessment_snapshot = stored.model_dump(mode="json")
    await db.flush()
    return row


async def get_student_billing_account(
    db: AsyncSession,
    student_id: uuid.UUID,
    term_id: uuid.UUID,
    *,
    for_update: bool = False,
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
               student_billing_accounts.due_date,
               student_billing_accounts.assessment_snapshot,
               student_billing_accounts.overdue_notified_at,
               student_billing_accounts.late_penalty_amount,
               student_billing_accounts.late_penalty_applied_at
        FROM student_billing_accounts
        WHERE student_billing_accounts.student_id = :student_id
          AND student_billing_accounts.academic_term_id = :term_id
        LIMIT 1; -- FOR UPDATE when the caller stages transitions.
    """
    stmt = (
        select(StudentBillingAccount)
        .where(
            StudentBillingAccount.student_id == student_id,
            StudentBillingAccount.academic_term_id == term_id,
        )
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return await db.scalar(stmt)


async def get_total_outstanding_balance(
    db: AsyncSession, student_id: uuid.UUID
) -> Decimal:
    """Return positive balances; credits and stale status labels cannot hide debt.

    SQL::

        SELECT COALESCE(SUM(student_billing_accounts.balance), 0)
        FROM student_billing_accounts
        WHERE student_billing_accounts.student_id = :student_id
          AND student_billing_accounts.balance > 0
    """
    stmt = select(func.coalesce(func.sum(StudentBillingAccount.balance), 0)).where(
        StudentBillingAccount.student_id == student_id,
        StudentBillingAccount.balance > 0,
    )
    res = await db.scalar(stmt)
    return Decimal(str(res)) if res is not None else Decimal("0.00")


async def list_student_payments(
    db: AsyncSession, student_id: uuid.UUID
) -> list[Payment]:
    """List only the caller's payments, newest first with deterministic ties.

    SQL::

        SELECT p.* FROM payments p
        JOIN student_billing_accounts a ON a.id = p.billing_account_id
        WHERE a.student_id = :student_id
        ORDER BY COALESCE(p.paid_at, p.created_at) DESC, p.created_at DESC, p.id DESC;
    """
    return list(
        await db.scalars(
            select(Payment)
            .join(
                StudentBillingAccount,
                StudentBillingAccount.id == Payment.billing_account_id,
            )
            .where(StudentBillingAccount.student_id == student_id)
            .order_by(
                func.coalesce(Payment.paid_at, Payment.created_at).desc(),
                Payment.created_at.desc(),
                Payment.id.desc(),
            )
        )
    )
