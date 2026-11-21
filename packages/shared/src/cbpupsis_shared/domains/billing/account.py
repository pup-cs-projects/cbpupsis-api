"""Student-owned account presentation over the existing shared assessment."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.academics import client as academics
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.billing import repository, service
from cbpupsis_shared.domains.billing.assessment import (
    covered_fee_codes,
    refresh_current_subsidy,
)
from cbpupsis_shared.domains.billing.constants import LINE_DESCRIPTIONS
from cbpupsis_shared.domains.billing.exceptions import BillingAccountUnavailableError
from cbpupsis_shared.domains.billing.overdue import (
    MANILA,
    account_balance,
    stage_overdue,
)
from cbpupsis_shared.domains.billing.schemas import (
    AccountLineRead,
    AccountRead,
    PaymentRead,
)
from cbpupsis_shared.domains.users import client as users


async def read_account(
    db: AsyncSession, student_id: uuid.UUID, *, as_of: datetime | None = None
) -> AccountRead:
    """Own the request transaction: audit/outbox/marker all commit or all roll back."""
    try:
        result = await stage_account_read(db, student_id, as_of=as_of)
        await db.commit()
        return result
    except Exception:
        await db.rollback()
        raise


async def stage_account_read(
    db: AsyncSession, student_id: uuid.UUID, *, as_of: datetime | None = None
) -> AccountRead:
    """Stage account-read side effects without committing the caller's work."""
    profile = await users.get_student_profile(db, student_id)
    if profile is None:
        raise ResourceNotFoundError
    term = await academics.get_current_term(db)
    if term is None:
        raise ResourceNotFoundError
    stored = await refresh_current_subsidy(db, profile, term.id)
    row = await repository.get_student_billing_account(
        db, student_id, term.id, for_update=True
    )
    if row is None:
        raise ResourceNotFoundError
    if stored is None:
        raise BillingAccountUnavailableError
    assessment = stored.assessment
    now = as_of or datetime.now(UTC)
    await stage_overdue(db, row, as_of=now)
    balance = account_balance(row)
    status = "DUE"
    if balance == 0:
        status = "PAID"
    elif now.astimezone(MANILA).date() > row.due_date:
        status = "OVERDUE"
    elif row.amount_paid > 0:
        status = "PARTIAL"
    covered = covered_fee_codes(stored.inputs)
    assessed = Decimal(assessment.amountAssessed) + row.late_penalty_amount
    return AccountRead(
        amountAssessed=f"{assessed:.2f}",
        subsidyApplied=assessment.subsidyApplied,
        netPayable=f"{balance:.2f}",
        paymentStatus=status,
        holds=await service.get_student_holds(
            db,
            student_id=student_id,
            has_academic_hold=profile.has_academic_hold,
            as_of=now,
            # Reuse the decision already staged under this account's lock.
            reconcile_financials=False,
        ),
        lines=[
            AccountLineRead(
                description=LINE_DESCRIPTIONS[line.code],
                amount=line.amount,
                coveredByFhe=line.code in covered,
            )
            for line in assessment.lines
        ]
        + (
            [
                AccountLineRead(
                    description="Late payment fine",
                    amount=f"{row.late_penalty_amount:.2f}",
                    coveredByFhe=False,
                )
            ]
            if row.late_penalty_applied_at is not None
            else []
        ),
        fheQualified=assessment.fheQualified,
        fheForfeited=assessment.fheForfeited,
        fheForfeitureReason=assessment.fheForfeitureReason,
    )


async def read_payments(db: AsyncSession, student_id: uuid.UUID) -> list[PaymentRead]:
    if await users.get_student_profile(db, student_id) is None:
        raise ResourceNotFoundError
    return [
        PaymentRead(
            date=(row.paid_at or row.created_at).replace(tzinfo=UTC)
            if (row.paid_at or row.created_at).tzinfo is None
            else row.paid_at or row.created_at,
            amount=f"{row.amount:.2f}",
            reference=row.gateway_reference or row.merchant_reference,
            status=row.payment_status,
        )
        for row in await repository.list_student_payments(db, student_id)
    ]
