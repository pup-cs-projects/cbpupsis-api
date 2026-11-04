from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.enrollment import repository
from cbpupsis_shared.domains.enrollment.constants import (
    MAX_OVERLOAD_UNITS,
    SUMMER_RESTRICTED_ADMISSION_STATUSES,
    SUMMER_SUBJECT_CAP,
)
from cbpupsis_shared.domains.enrollment.exceptions import (
    OverloadCapExceededError,
    SummerSubjectLimitError,
    UnitCeilingExceededError,
)
from cbpupsis_shared.domains.enrollment.schemas import (
    CeilingCheckRead,
    CeilingSection,
    CeilingStudent,
    CeilingTerm,
    EnrolledSection,
    EnrollmentSummaryRead,
    RecordEnrollment,
    TermEnrollmentRead,
)


def compute_effective_ceiling(student: CeilingStudent) -> Decimal:
    return (
        student.prescribed_load
        + student.approved_overload_units
        - student.standing_reduction
    )


def check_unit_ceiling(
    student: CeilingStudent,
    term: CeilingTerm,
    sections: list[CeilingSection],
) -> CeilingCheckRead:
    attempted_units = sum((section.units for section in sections), Decimal("0"))
    overload_cap = student.prescribed_load + MAX_OVERLOAD_UNITS
    if attempted_units > overload_cap:
        raise OverloadCapExceededError(maximum_overload_units=MAX_OVERLOAD_UNITS)

    effective_ceiling = compute_effective_ceiling(student)
    if attempted_units > effective_ceiling:
        raise UnitCeilingExceededError(
            prescribed_load=student.prescribed_load,
            approved_overload_units=student.approved_overload_units,
            standing_reduction=student.standing_reduction,
            effective_ceiling=effective_ceiling,
            attempted_units=attempted_units,
        )

    restricted_admission = (
        student.admission_status.strip().upper() in SUMMER_RESTRICTED_ADMISSION_STATUSES
    )
    has_back_subject = any(section.is_back_subject for section in sections)
    if (
        term.term_type == "SUMMER"
        and (restricted_admission or has_back_subject)
        and len(sections) > SUMMER_SUBJECT_CAP
    ):
        raise SummerSubjectLimitError(subject_cap=SUMMER_SUBJECT_CAP)

    return CeilingCheckRead(
        attempted_units=attempted_units,
        effective_ceiling=effective_ceiling,
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
    enrolled_units, course_count = await repository.get_enrolled_units_and_course_count(
        db, student_id=student_id, academic_term_id=academic_term_id
    )
    approved_overload = await repository.get_approved_overload_units(
        db, student_id=student_id, academic_term_id=academic_term_id
    )

    effective_ceiling = compute_effective_ceiling(
        CeilingStudent(
            prescribed_load=prescribed_load,
            approved_overload_units=approved_overload,
            standing_reduction=standing_reduction,
        )
    )

    return EnrollmentSummaryRead(
        enrolled_units=enrolled_units,
        effective_ceiling=effective_ceiling,
        prescribed_load=prescribed_load,
        approved_overload_units=approved_overload,
        standing_reduction=standing_reduction,
        enrolled_course_count=course_count,
        schedule_url="/api/v1/student/schedule",
    )


async def get_approved_overload_units(
    db: AsyncSession, *, student_id: uuid.UUID, academic_term_id: uuid.UUID
) -> Decimal:
    return await repository.get_approved_overload_units(
        db, student_id=student_id, academic_term_id=academic_term_id
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
