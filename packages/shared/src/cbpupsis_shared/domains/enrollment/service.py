"""Enrollment reads and load summaries, independent of approval workflows."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.enrollment import repository
from cbpupsis_shared.domains.enrollment.schemas import (
    EnrolledSection,
    EnrollmentSummaryRead,
    RecordEnrollment,
    TermEnrollmentRead,
)


async def get_term_enrollment(
    db: AsyncSession, student_id: uuid.UUID, academic_term_id: uuid.UUID
) -> TermEnrollmentRead | None:
    row = await repository.get_term_enrollment(db, student_id, academic_term_id)
    if row is None:
        return None
    return TermEnrollmentRead.model_validate(row)


async def get_enrollment_summary(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    academic_term_id: uuid.UUID,
    prescribed_load: Decimal,
    standing_reduction: Decimal,
) -> EnrollmentSummaryRead:
    """Return enrollment metrics against effective ceiling computed from curriculum.

    effectiveCeiling = prescribedLoad + approvedOverload - standingReduction
    """
    enrolled_units, course_count = await repository.get_enrolled_units_and_course_count(
        db, student_id=student_id, academic_term_id=academic_term_id
    )
    approved_overload = await repository.get_approved_overload_units(
        db, student_id=student_id, academic_term_id=academic_term_id
    )

    # Computed: prescribedLoad + approvedOverload - standingReduction
    effective_ceiling = prescribed_load + approved_overload - standing_reduction

    return EnrollmentSummaryRead(
        enrolled_units=enrolled_units,
        effective_ceiling=effective_ceiling,
        prescribed_load=prescribed_load,
        approved_overload_units=approved_overload,
        standing_reduction=standing_reduction,
        enrolled_course_count=course_count,
        schedule_url="/api/v1/student/schedule",
    )


async def record_enrollments(db: AsyncSession, student_id: uuid.UUID):
    return [
        RecordEnrollment.model_validate(row, from_attributes=True)
        for row in await repository.list_record_enrollments(db, student_id)
    ]


async def enrolled_sections(
    db: AsyncSession, student_id: uuid.UUID, term_id: uuid.UUID
) -> list[EnrolledSection]:
    return [
        EnrolledSection.model_validate(row)
        for row in await repository.enrolled_sections(db, student_id, term_id)
    ]
