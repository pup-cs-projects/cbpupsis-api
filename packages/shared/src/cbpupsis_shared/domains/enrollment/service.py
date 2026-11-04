from __future__ import annotations

import uuid
from datetime import time
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.academics.schemas import AcademicTermRead
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.curriculum import client as curriculum
from cbpupsis_shared.domains.enrollment import repository
from cbpupsis_shared.domains.enrollment.constants import (
    MAX_OVERLOAD_UNITS,
    SUMMER_RESTRICTED_ADMISSION_STATUSES,
    SUMMER_SUBJECT_CAP,
)
from cbpupsis_shared.domains.enrollment.exceptions import (
    OverloadCapExceededError,
    ScheduleConflictError,
    SummerSubjectLimitError,
    UnitCeilingExceededError,
)
from cbpupsis_shared.domains.enrollment.schemas import (
    AvailableCourseRead,
    AvailableCourseScheduleRead,
    CeilingCheckRead,
    CeilingSection,
    CeilingStudent,
    CeilingTerm,
    ConflictSection,
    ConflictStudent,
    ConflictTerm,
    EnrolledSection,
    EnrollmentSummaryRead,
    RecordEnrollment,
    ScheduleConflictCheckRead,
    TermEnrollmentRead,
)
from cbpupsis_shared.domains.sections import client as sections
from cbpupsis_shared.domains.users import client as users


def _overlap(
    first_day: int,
    first_start: time,
    first_end: time,
    second_day: int,
    second_start: time,
    second_end: time,
) -> tuple[time, time] | None:
    if first_day != second_day:
        return None
    start = max(first_start, second_start)
    end = min(first_end, second_end)
    return (start, end) if start < end else None


def _sections_overlap(first, second) -> bool:
    return any(
        _overlap(
            first_meeting.day_of_week,
            first_meeting.start_time,
            first_meeting.end_time,
            second_meeting.day_of_week,
            second_meeting.start_time,
            second_meeting.end_time,
        )
        is not None
        for first_meeting in first.meetings
        for second_meeting in second.meetings
    )


def check_schedule_conflict(
    student: ConflictStudent,
    term: ConflictTerm,
    selected_sections: list[ConflictSection],
) -> ScheduleConflictCheckRead:
    """Reject the first overlapping pair using half-open meeting intervals."""
    _ = student
    if any(
        section.academic_term_id != term.academic_term_id
        for section in selected_sections
    ):
        raise ValueError("Every section must belong to the selected academic term.")
    for index, first in enumerate(selected_sections):
        for second in selected_sections[index + 1 :]:
            for first_meeting in first.schedule:
                for second_meeting in second.schedule:
                    overlap = _overlap(
                        first_meeting.day,
                        first_meeting.start,
                        first_meeting.end,
                        second_meeting.day,
                        second_meeting.start,
                        second_meeting.end,
                    )
                    if overlap is not None:
                        raise ScheduleConflictError(
                            first_section_id=first.section_id,
                            first_course_code=first.course_code,
                            second_section_id=second.section_id,
                            second_course_code=second.course_code,
                            day=first_meeting.day,
                            start=overlap[0],
                            end=overlap[1],
                        )
    return ScheduleConflictCheckRead()


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


async def list_available_courses(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    term: AcademicTermRead,
) -> list[AvailableCourseRead]:
    profile = await users.get_student_profile(db, student_id)
    if profile is None:
        raise ResourceNotFoundError

    course_rows = await curriculum.enrollment_courses(
        db,
        curriculum_id=profile.curriculum_id,
        program_id=profile.program_id,
        year_level=profile.year_level,
        semester=term.semester,
    )
    courses_by_id = {course.id: course for course in course_rows}
    section_rows = await sections.available_sections(
        db,
        academic_term_id=term.id,
        course_ids=courses_by_id.keys(),
    )
    if not section_rows:
        return []

    section_ids = [section.id for section in section_rows]
    seat_counts = await repository.count_active_section_enrollments(db, section_ids)
    enrolled = await repository.enrolled_sections(db, student_id, term.id)
    enrolled_schedules = await sections.schedule_sections(
        db, [row.section_id for row in enrolled]
    )
    faculty_names = await users.schedule_names(
        db,
        list(
            {
                section.faculty_id
                for section in section_rows
                if section.faculty_id is not None
            }
        ),
    )

    available: list[AvailableCourseRead] = []
    for section in section_rows:
        course = courses_by_id.get(section.course_id)
        if course is None:
            continue
        conflicts = sorted(
            (
                enrolled_section.id
                for enrolled_section in enrolled_schedules
                if enrolled_section.id != section.id
                and _sections_overlap(section, enrolled_section)
            ),
            key=str,
        )
        occupied = seat_counts.get(section.id, 0)
        available.append(
            AvailableCourseRead(
                section_id=section.id,
                course_code=course.course_code,
                title=course.title,
                units=course.lecture_units + course.lab_units,
                schedule=[
                    AvailableCourseScheduleRead(
                        day=meeting.day_of_week,
                        start=meeting.start_time,
                        end=meeting.end_time,
                        room=meeting.room,
                    )
                    for meeting in section.meetings
                ],
                faculty=(
                    faculty_names.get(section.faculty_id)
                    if section.faculty_id is not None
                    else None
                ),
                capacity=section.capacity,
                available_slots=max(section.capacity - occupied, 0),
                prerequisites_satisfied=True,
                block_reason=None,
                conflicts_with=conflicts,
            )
        )
    return sorted(
        available,
        key=lambda item: (item.course_code, str(item.section_id)),
    )
