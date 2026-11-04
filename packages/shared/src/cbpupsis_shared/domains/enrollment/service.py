from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.events import Event
from cbpupsis_shared.domains.academics.schemas import AcademicTermRead
from cbpupsis_shared.domains.audit import client as audit
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.billing import client as billing
from cbpupsis_shared.domains.curriculum import client as curriculum
from cbpupsis_shared.domains.enrollment import repository
from cbpupsis_shared.domains.enrollment.constants import (
    BLOCKED_ACADEMIC_STANDINGS,
    ENROLLED_PENDING,
    ENROLLMENT_SUBMITTED_ACTION,
    ENROLLMENT_SUBMITTED_EVENT,
    MAX_OVERLOAD_UNITS,
    SUMMER_RESTRICTED_ADMISSION_STATUSES,
    SUMMER_SUBJECT_CAP,
)
from cbpupsis_shared.domains.enrollment.exceptions import (
    EnrollmentFinancialHoldError,
    EnrollmentHoldActiveError,
    EnrollmentPeriodClosedError,
    EnrollmentSectionFullError,
    EnrollmentStandingBlockedError,
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
    EnrollmentSubmitRead,
    EnrollmentSubmitSectionRead,
    EnrollmentSummaryRead,
    RecordEnrollment,
    ScheduleConflictCheckRead,
    TermEnrollmentRead,
)
from cbpupsis_shared.domains.grading import client as grading
from cbpupsis_shared.domains.sections import client as sections
from cbpupsis_shared.domains.users import client as users
from cbpupsis_shared.outbox import publish_transactional

_SQLITE_SUBMISSION_LOCK = asyncio.Lock()


@asynccontextmanager
async def _submission_guard(db: AsyncSession):
    """Serialize SQLite submissions; PostgreSQL uses row locks across instances."""
    if db.get_bind().dialect.name == "sqlite":
        async with _SQLITE_SUBMISSION_LOCK:
            yield
    else:
        yield


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _enrollment_period(term: AcademicTermRead) -> tuple[datetime, datetime]:
    start = term.early_enrollment_start or term.regular_enrollment_start
    end = term.late_enrollment_end or term.regular_enrollment_end
    if start is None or end is None:
        raise ResourceNotFoundError
    return _as_utc(start), _as_utc(end)


def _term_type(term: AcademicTermRead) -> str:
    return "SUMMER" if "summer" in term.semester.lower() else "REGULAR"


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


async def _submission_response(
    db: AsyncSession, enrollment_row
) -> EnrollmentSubmitRead:
    registrations = await repository.list_term_enrollment_sections(
        db, enrollment_row.id
    )
    section_rows = await sections.schedule_sections(
        db, [row.section_id for row in registrations]
    )
    course_rows = {
        row.id: row
        for row in await curriculum.schedule_courses(
            db, [section.course_id for section in section_rows]
        )
    }
    submitted_sections = []
    for section in sorted(section_rows, key=lambda row: str(row.id)):
        course = course_rows.get(section.course_id)
        if course is None:
            raise ResourceNotFoundError
        submitted_sections.append(
            EnrollmentSubmitSectionRead(
                section_id=section.id,
                course_code=course.course_code,
                title=course.title,
                units=course.lecture_units + course.lab_units,
            )
        )
    return EnrollmentSubmitRead(
        enrollment_id=enrollment_row.id,
        status=ENROLLED_PENDING,
        total_units=sum(
            (section.units for section in submitted_sections), Decimal("0")
        ),
        sections=submitted_sections,
    )


def _selected_conflict_sections(term, section_rows, courses_by_id):
    return [
        ConflictSection(
            section_id=section.id,
            course_code=courses_by_id[section.course_id].course_code,
            academic_term_id=term.id,
            schedule=[
                {
                    "day": meeting.day_of_week,
                    "start": meeting.start_time,
                    "end": meeting.end_time,
                }
                for meeting in section.meetings
            ],
        )
        for section in section_rows
    ]


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


async def submit_enrollment(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    term: AcademicTermRead,
    section_ids: list[uuid.UUID],
    client_ip: str | None,
    role: str = "student",
    as_of: datetime | None = None,
) -> EnrollmentSubmitRead:
    """Assess and reserve all selected seats in one transaction."""
    async with _submission_guard(db):
        existing = await repository.get_term_enrollment(db, student_id, term.id)
        if existing is not None:
            return await _submission_response(db, existing)

        try:
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
            selected = await sections.schedule_sections(db, section_ids)
            selected_by_id = {section.id: section for section in selected}
            if set(selected_by_id) != set(section_ids) or any(
                section.academic_term_id != term.id
                or section.course_id not in courses_by_id
                or section.status.lower() != "open"
                for section in selected
            ):
                raise ResourceNotFoundError

            period_start, period_end = _enrollment_period(term)
            now = _as_utc(as_of or datetime.now(UTC))
            if not period_start <= now <= period_end:
                raise EnrollmentPeriodClosedError(
                    start=period_start.isoformat(), end=period_end.isoformat()
                )

            holds = await billing.get_student_holds(
                db,
                student_id=student_id,
                has_financial_hold=profile.has_financial_hold,
                has_academic_hold=profile.has_academic_hold,
            )
            financial_hold = next(
                (
                    hold
                    for hold in holds
                    if hold.is_blocking and hold.type.upper() == "FINANCIAL"
                ),
                None,
            )
            if financial_hold is not None:
                raise EnrollmentFinancialHoldError(
                    office=financial_hold.office,
                    amount=financial_hold.amount_outstanding,
                )
            active_hold = next((hold for hold in holds if hold.is_blocking), None)
            if active_hold is not None:
                raise EnrollmentHoldActiveError(
                    office=active_hold.office, hold_type=active_hold.type
                )

            standing = await grading.get_student_scholastic_standing(db, student_id)
            if standing.value.upper() in BLOCKED_ACADEMIC_STANDINGS:
                raise EnrollmentStandingBlockedError(standing=standing.value)

            prescribed_load = await curriculum.get_prescribed_load(
                db,
                curriculum_id=profile.curriculum_id,
                year_level=profile.year_level,
                semester=term.semester,
            )
            approved_overload = await repository.get_approved_overload_units(
                db, student_id=student_id, academic_term_id=term.id
            )
            check_unit_ceiling(
                CeilingStudent(
                    prescribed_load=prescribed_load,
                    approved_overload_units=approved_overload,
                    standing_reduction=Decimal(standing.unit_reduction),
                    admission_status=profile.admission_status,
                ),
                CeilingTerm(term_type=_term_type(term)),
                [
                    CeilingSection(
                        units=(
                            courses_by_id[section.course_id].lecture_units
                            + courses_by_id[section.course_id].lab_units
                        )
                    )
                    for section in selected
                ],
            )

            check_schedule_conflict(
                ConflictStudent(student_id=student_id),
                ConflictTerm(academic_term_id=term.id),
                _selected_conflict_sections(term, selected, courses_by_id),
            )

            locked = await sections.lock_for_enrollment(db, section_ids)
            locked_by_id = {section.id: section for section in locked}
            if set(locked_by_id) != set(section_ids) or any(
                section.academic_term_id != term.id
                or section.course_id not in courses_by_id
                or section.status.lower() != "open"
                for section in locked
            ):
                raise ResourceNotFoundError

            existing = await repository.get_term_enrollment(db, student_id, term.id)
            if existing is not None:
                await db.rollback()
                existing = await repository.get_term_enrollment(db, student_id, term.id)
                if existing is None:
                    raise ResourceNotFoundError
                return await _submission_response(db, existing)

            occupied = await repository.count_active_section_enrollments(
                db, section_ids
            )
            for section_id in sorted(section_ids, key=str):
                if occupied.get(section_id, 0) >= locked_by_id[section_id].capacity:
                    raise EnrollmentSectionFullError(section_id=section_id)

            enrollment_row = repository.add_term_enrollment(
                db,
                student_id=student_id,
                academic_term_id=term.id,
                status=ENROLLED_PENDING,
            )
            await db.flush()
            repository.add_course_enrollments(
                db,
                term_enrollment_id=enrollment_row.id,
                academic_term_id=term.id,
                section_ids=sorted(section_ids, key=str),
            )

            response = await _submission_response(db, enrollment_row)
            occurred_at = datetime.now(UTC)
            section_id_strings = [str(item.section_id) for item in response.sections]
            audit.stage(
                db,
                action=ENROLLMENT_SUBMITTED_ACTION,
                actor_id=student_id,
                target_type="term_enrollment",
                target_id=enrollment_row.id,
                occurred_at=occurred_at,
                prior_state={"status": None, "sectionIds": []},
                new_state={
                    "status": ENROLLED_PENDING,
                    "sectionIds": section_id_strings,
                    "totalUnits": str(response.total_units),
                },
                payload={
                    "actor": str(student_id),
                    "role": role,
                    "timestamp": occurred_at.isoformat(),
                    "ip_address": client_ip,
                },
            )
            publish_transactional(
                db,
                Event(
                    name=ENROLLMENT_SUBMITTED_EVENT,
                    payload={
                        "user_id": str(student_id),
                        "enrollment_id": str(enrollment_row.id),
                        "academic_term_id": str(term.id),
                        "status": ENROLLED_PENDING,
                        "section_ids": section_id_strings,
                        "total_units": str(response.total_units),
                    },
                ),
            )
            await db.commit()
            return response
        except IntegrityError:
            await db.rollback()
            existing = await repository.get_term_enrollment(db, student_id, term.id)
            if existing is not None:
                return await _submission_response(db, existing)
            raise
        except Exception:
            await db.rollback()
            raise
