"""Data access for the billing domain.

Only this module builds SQL or calls into the session for billing models.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.billing import (
    FeeStructure,
    Payment,
    PaymentReconciliationRun,
    PaymentWebhookEvent,
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


async def list_term_ids_with_fee_structures(db: AsyncSession) -> set[uuid.UUID]:
    """Return the IDs of terms that have at least one fee structure row.

    SQL::

        SELECT DISTINCT academic_term_id FROM fee_structures;
    """
    return set(await db.scalars(select(FeeStructure.academic_term_id).distinct()))


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


async def get_payment_by_idempotency(
    db: AsyncSession, student_id: uuid.UUID, key: str
) -> Payment | None:
    """Read the attempt previously created for one student's key.

    SQL:: SELECT * FROM payments WHERE student_id=:student AND idempotency_key=:key;
    """
    return await db.scalar(
        select(Payment).where(
            Payment.student_id == student_id, Payment.idempotency_key == key
        )
    )


async def lock_outstanding_account(
    db: AsyncSession, student_id: uuid.UUID
) -> StudentBillingAccount | None:
    """Lock the oldest account that still has a balance.

    SQL:: SELECT * FROM student_billing_accounts WHERE student_id=:student
    AND balance > 0 ORDER BY due_date, id LIMIT 1 FOR UPDATE;
    """
    return await db.scalar(
        select(StudentBillingAccount)
        .where(
            StudentBillingAccount.student_id == student_id,
            StudentBillingAccount.balance > 0,
        )
        .order_by(StudentBillingAccount.due_date, StudentBillingAccount.id)
        .limit(1)
        .with_for_update()
    )


async def pending_reservations(
    db: AsyncSession, billing_account_id: uuid.UUID
) -> Decimal:
    """Sum unsettled attempts so concurrent checkouts cannot over-reserve.

    SQL:: SELECT COALESCE(SUM(amount), 0) FROM payments
    WHERE billing_account_id=:account AND payment_status='PENDING';
    """
    value = await db.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.billing_account_id == billing_account_id,
            Payment.payment_status == "PENDING",
        )
    )
    return Decimal(str(value or 0))


def stage_payment(
    db: AsyncSession,
    *,
    payment_id: uuid.UUID,
    billing_account_id: uuid.UUID,
    student_id: uuid.UUID,
    idempotency_key: str,
    merchant_reference: str,
    amount: Decimal,
    initiated_ip: str | None,
) -> Payment:
    """Stage one durable payment attempt before contacting Xendit.

    SQL:: INSERT INTO payments (id, billing_account_id, student_id,
    idempotency_key, merchant_reference, amount, initiated_ip) VALUES (...);
    """
    row = Payment(
        id=payment_id,
        billing_account_id=billing_account_id,
        student_id=student_id,
        idempotency_key=idempotency_key,
        merchant_reference=merchant_reference,
        amount=amount,
        initiated_ip=initiated_ip,
    )
    db.add(row)
    return row


async def get_student_payment(
    db: AsyncSession, student_id: uuid.UUID, payment_id: uuid.UUID
) -> Payment | None:
    """Read a payment only when it belongs to the requesting student.

    SQL:: SELECT * FROM payments WHERE id=:id AND student_id=:student;
    """
    return await db.scalar(
        select(Payment).where(
            Payment.id == payment_id, Payment.student_id == student_id
        )
    )


async def lock_payment_by_merchant_reference(
    db: AsyncSession, merchant_reference: str
) -> Payment | None:
    """Lock the payment named by the non-secret merchant reference.

    SQL:: SELECT * FROM payments WHERE merchant_reference=:reference FOR UPDATE;
    """
    return await db.scalar(
        select(Payment)
        .where(Payment.merchant_reference == merchant_reference)
        .with_for_update()
    )


async def lock_payment(db: AsyncSession, payment_id: uuid.UUID) -> Payment | None:
    """Lock a payment while settlement changes its account balance.

    SQL:: SELECT * FROM payments WHERE id=:id FOR UPDATE;
    """
    return await db.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )


async def lock_billing_account(
    db: AsyncSession, billing_account_id: uuid.UUID
) -> StudentBillingAccount | None:
    """Lock the account whose paid amount is about to change.

    SQL:: SELECT * FROM student_billing_accounts WHERE id=:id FOR UPDATE;
    """
    return await db.scalar(
        select(StudentBillingAccount)
        .where(StudentBillingAccount.id == billing_account_id)
        .with_for_update()
    )


async def get_webhook_event(
    db: AsyncSession, webhook_id: str
) -> PaymentWebhookEvent | None:
    """Find a previously processed Xendit delivery.

    SQL:: SELECT * FROM payment_webhook_events WHERE webhook_id=:webhook_id;
    """
    return await db.scalar(
        select(PaymentWebhookEvent).where(PaymentWebhookEvent.webhook_id == webhook_id)
    )


def stage_webhook_event(
    db: AsyncSession,
    *,
    webhook_id: str,
    payment_id: uuid.UUID,
    event_type: str,
    event_created_at: datetime,
) -> PaymentWebhookEvent:
    """Stage a durable delivery receipt in the settlement transaction.

    SQL:: INSERT INTO payment_webhook_events
    (webhook_id, payment_id, event_type, event_created_at) VALUES (...);
    """
    row = PaymentWebhookEvent(
        webhook_id=webhook_id,
        payment_id=payment_id,
        event_type=event_type,
        event_created_at=event_created_at,
    )
    db.add(row)
    return row


async def list_stale_pending_payments(
    db: AsyncSession, cutoff: datetime, limit: int
) -> list[Payment]:
    """Lock a bounded batch of old pending attempts for reconciliation.

    SQL:: SELECT * FROM payments WHERE payment_status='PENDING'
    AND created_at <= :cutoff ORDER BY created_at, id LIMIT :limit
    FOR UPDATE SKIP LOCKED;
    """
    return list(
        await db.scalars(
            select(Payment)
            .where(Payment.payment_status == "PENDING", Payment.created_at <= cutoff)
            .order_by(Payment.created_at, Payment.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )


async def get_reconciliation_run(
    db: AsyncSession, run_key: str
) -> PaymentReconciliationRun | None:
    """Read a prior reconciliation result by scheduler run key.

    SQL:: SELECT * FROM payment_reconciliation_runs WHERE run_key=:run_key;
    """
    return await db.scalar(
        select(PaymentReconciliationRun).where(
            PaymentReconciliationRun.run_key == run_key
        )
    )


def stage_reconciliation_run(
    db: AsyncSession, run_key: str
) -> PaymentReconciliationRun:
    """Stage the unique marker that makes one scheduler run replay-safe.

    SQL:: INSERT INTO payment_reconciliation_runs (run_key) VALUES (:run_key);
    """
    row = PaymentReconciliationRun(run_key=run_key)
    db.add(row)
    return row
