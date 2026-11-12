"""Business logic for the billing domain."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.billing import repository
from cbpupsis_shared.domains.billing.schemas import HoldRead, StudentBillingAccountRead


async def get_student_billing(
    db: AsyncSession, student_id: uuid.UUID, term_id: uuid.UUID
) -> StudentBillingAccountRead | None:
    row = await repository.get_student_billing_account(db, student_id, term_id)
    if row is None:
        return None
    return StudentBillingAccountRead.model_validate(row)


async def get_total_outstanding_balance(
    db: AsyncSession, student_id: uuid.UUID
) -> Decimal:
    return await repository.get_total_outstanding_balance(db, student_id)


async def get_student_holds(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    has_financial_hold: bool = False,
    has_academic_hold: bool = False,
) -> list[HoldRead]:
    """Assemble all blocking and non-blocking holds for the student."""
    stored = await repository.list_unresolved_student_holds(db, student_id)
    holds = [
        HoldRead(
            type=row.hold_type,
            office=row.office,
            description=row.description,
            amount_outstanding=f"{(row.amount_outstanding or Decimal('0')):.2f}",
            currency=row.currency,
            is_blocking=row.blocks_enrollment,
            blocks=["ENROLLMENT"] if row.blocks_enrollment else [],
            blocks_enrollment=row.blocks_enrollment,
        )
        for row in stored
    ]
    stored_types = {hold.type.strip().upper() for hold in holds}

    if has_financial_hold and "FINANCIAL" not in stored_types:
        outstanding = await repository.get_total_outstanding_balance(db, student_id)
        holds.append(
            HoldRead(
                type="FINANCIAL",
                office="Accounting Office",
                description="Outstanding balance from previous term",
                amount_outstanding=f"{outstanding:.2f}",
                currency="PHP",
                is_blocking=True,
                blocks=["ENROLLMENT", "COR"],
                blocks_enrollment=True,
            )
        )

    if has_academic_hold and not stored_types.intersection(
        {"ACADEMIC", "ADMINISTRATIVE", "DISCIPLINARY"}
    ):
        holds.append(
            HoldRead(
                type="ACADEMIC",
                office="Registrar's Office",
                description="Academic document clearance hold",
                amount_outstanding="0.00",
                currency="PHP",
                is_blocking=True,
                blocks=["ENROLLMENT"],
                blocks_enrollment=True,
            )
        )

    return holds
