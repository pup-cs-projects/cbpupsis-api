"""Internal client for the billing domain: the extraction swap point."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.billing import service
from cbpupsis_shared.domains.billing.account import read_account, read_payments
from cbpupsis_shared.domains.billing.assessment import (
    AssessmentCourse,
    AssessmentInputs,
    AssessmentInvalid,
    AssessmentLine,
    AssessmentRead,
    StoredAssessment,
    calculate_assessment,
    read_stored_assessment,
    stage_assessment,
)
from cbpupsis_shared.domains.billing.schemas import HoldRead, StudentBillingAccountRead

__all__ = [
    "AssessmentCourse",
    "AssessmentInputs",
    "AssessmentInvalid",
    "AssessmentLine",
    "AssessmentRead",
    "HoldRead",
    "StoredAssessment",
    "StudentBillingAccountRead",
    "calculate_assessment",
    "get_student_billing",
    "get_student_holds",
    "get_total_outstanding_balance",
    "read_account",
    "read_payments",
    "read_stored_assessment",
    "stage_assessment",
]


async def get_student_billing(
    db: AsyncSession, student_id: uuid.UUID, term_id: uuid.UUID
) -> StudentBillingAccountRead | None:
    return await service.get_student_billing(db, student_id, term_id)


async def get_total_outstanding_balance(
    db: AsyncSession, student_id: uuid.UUID
) -> Decimal:
    return await service.get_total_outstanding_balance(db, student_id)


async def get_student_holds(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    has_financial_hold: bool = False,
    has_academic_hold: bool = False,
    as_of: datetime | None = None,
) -> list[HoldRead]:
    return await service.get_student_holds(
        db,
        student_id=student_id,
        has_financial_hold=has_financial_hold,
        has_academic_hold=has_academic_hold,
        as_of=as_of,
    )
