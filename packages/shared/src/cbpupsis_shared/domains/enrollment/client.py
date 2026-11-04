from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.enrollment import service
from cbpupsis_shared.domains.enrollment.schemas import (
    EnrollmentSummaryRead,
    TermEnrollmentRead,
)
from cbpupsis_shared.domains.enrollment.service import (
    check_unit_ceiling,
    compute_effective_ceiling,
    enrolled_sections,
    record_enrollments,
)

__all__ = [
    "EnrollmentSummaryRead",
    "TermEnrollmentRead",
    "check_unit_ceiling",
    "compute_effective_ceiling",
    "enrolled_sections",
    "get_approved_overload_units",
    "get_enrollment_summary",
    "get_term_enrollment",
    "record_enrollments",
]


async def get_term_enrollment(
    db: AsyncSession, student_id: uuid.UUID, academic_term_id: uuid.UUID
) -> TermEnrollmentRead | None:
    return await service.get_term_enrollment(db, student_id, academic_term_id)


async def get_enrollment_summary(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    academic_term_id: uuid.UUID,
    prescribed_load: Decimal,
    standing_reduction: Decimal,
) -> EnrollmentSummaryRead:
    return await service.get_enrollment_summary(
        db,
        student_id=student_id,
        academic_term_id=academic_term_id,
        prescribed_load=prescribed_load,
        standing_reduction=standing_reduction,
    )


async def get_approved_overload_units(
    db: AsyncSession, *, student_id: uuid.UUID, academic_term_id: uuid.UUID
) -> Decimal:
    return await service.get_approved_overload_units(
        db, student_id=student_id, academic_term_id=academic_term_id
    )
