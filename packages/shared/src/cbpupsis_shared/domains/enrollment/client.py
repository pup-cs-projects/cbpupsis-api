from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.academics.schemas import AcademicTermRead
from cbpupsis_shared.domains.enrollment import service
from cbpupsis_shared.domains.enrollment.schemas import (
    AvailableCourseRead,
    EnrollmentSubmitRead,
    EnrollmentSummaryRead,
    TermEnrollmentRead,
)
from cbpupsis_shared.domains.enrollment.service import (
    check_schedule_conflict,
    check_unit_ceiling,
    compute_effective_ceiling,
    enrolled_sections,
    record_enrollments,
)

__all__ = [
    "AvailableCourseRead",
    "EnrollmentSubmitRead",
    "EnrollmentSummaryRead",
    "TermEnrollmentRead",
    "check_schedule_conflict",
    "check_unit_ceiling",
    "compute_effective_ceiling",
    "enrolled_sections",
    "get_approved_overload_units",
    "get_enrollment_summary",
    "get_term_enrollment",
    "list_available_courses",
    "record_enrollments",
    "submit_enrollment",
]


async def list_available_courses(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    term: AcademicTermRead,
) -> list[AvailableCourseRead]:
    return await service.list_available_courses(db, student_id=student_id, term=term)


async def submit_enrollment(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    term: AcademicTermRead,
    section_ids: list[uuid.UUID],
    client_ip: str | None,
    role: str = "student",
) -> EnrollmentSubmitRead:
    return await service.submit_enrollment(
        db,
        student_id=student_id,
        term=term,
        section_ids=section_ids,
        client_ip=client_ip,
        role=role,
    )


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
