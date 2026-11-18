from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, time
from decimal import Decimal

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_core.events import Event
from cbpupsis_core.exceptions import ConflictError, ForbiddenError, ValidationError
from cbpupsis_shared.domains.academics import client as academics
from cbpupsis_shared.domains.academics.schemas import AcademicTermRead
from cbpupsis_shared.domains.audit import client as audit
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.billing import client as billing
from cbpupsis_shared.domains.curriculum import client as curriculum
from cbpupsis_shared.domains.enrollment import academic, repository
from cbpupsis_shared.domains.enrollment.certificate import stage_reissue
from cbpupsis_shared.domains.enrollment.constants import (
    ACTIVE_COURSE_ENROLLMENT_STATUSES,
    ADD_DROP_APPROVED,
    ADD_DROP_COURSE_TAGS,
    ADD_DROP_DECISION_ATTEMPTS,
    ADD_DROP_PENDING,
    ADD_DROP_REJECTED,
    ADD_DROP_REQUEST_DECIDED_ACTION,
    ADD_DROP_REQUEST_DECIDED_EVENT,
    ADD_DROP_REQUEST_FILED_ACTION,
    BLOCKED_ACADEMIC_STANDINGS,
    ENROLLED_PENDING,
    ENROLLMENT_SUBMITTED_ACTION,
    ENROLLMENT_SUBMITTED_EVENT,
    MAX_OVERLOAD_UNITS,
    MINIMUM_REGULAR_LOAD,
    OVERLOAD_APPROVED,
    OVERLOAD_GRADUATING_RATIO,
    OVERLOAD_PENDING,
    OVERLOAD_REJECTED,
    OVERLOAD_REQUEST_DECIDED_EVENT,
    OVERLOAD_REQUEST_FILED_ACTION,
    OVERLOAD_REQUIRED_GWA,
    SUMMER_RESTRICTED_ADMISSION_STATUSES,
    SUMMER_SUBJECT_CAP,
)
from cbpupsis_shared.domains.enrollment.exceptions import (
    AddDropPeriodClosedError,
    BelowMinimumLoadError,
    CorNotFinal,
    DecisionRemarksRequiredError,
    EnrollmentAssessmentUnavailableError,
    EnrollmentFinancialHoldError,
    EnrollmentHoldActiveError,
    EnrollmentPeriodClosedError,
    EnrollmentSectionFullError,
    EnrollmentStandingBlockedError,
    OverloadCapExceededError,
    OverloadNotEligibleError,
    RequestAlreadyDecidedError,
    ScheduleConflictError,
    SummerSubjectLimitError,
    UnitCeilingExceededError,
)
from cbpupsis_shared.domains.enrollment.schemas import (
    AddDropCourseRead,
    AddDropRequestCreate,
    AddDropRequestRead,
    AdminAddDropRequestRead,
    AvailableCourseRead,
    AvailableCourseScheduleRead,
    CeilingCheckRead,
    CeilingSection,
    CeilingStudent,
    CeilingTerm,
    ConflictSection,
    ConflictStudent,
    ConflictTerm,
    CurrentEnrollmentRead,
    EnrolledSection,
    EnrollmentSubmitRead,
    EnrollmentSubmitSectionRead,
    EnrollmentSummaryRead,
    OverloadEligibilityRead,
    OverloadRequestFiledRead,
    OverloadRequestRead,
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


async def get_current_enrollment(
    db: AsyncSession, student_id: uuid.UUID, academic_term_id: uuid.UUID
) -> CurrentEnrollmentRead | None:
    row = await repository.get_term_enrollment(db, student_id, academic_term_id)
    if row is None:
        return None
    held = [
        seat.section_id
        for seat in await repository.list_term_enrollment_sections(db, row.id)
        if seat.status in ACTIVE_COURSE_ENROLLMENT_STATUSES
    ]
    section_rows = await sections.schedule_sections(db, held)
    courses = {
        course.id: course
        for course in await curriculum.schedule_courses(
            db, [section.course_id for section in section_rows]
        )
    }
    total = sum(
        (
            courses[section.course_id].lecture_units
            + courses[section.course_id].lab_units
            for section in section_rows
            if section.course_id in courses
        ),
        Decimal("0"),
    )
    return CurrentEnrollmentRead(
        enrollment_id=row.id, status=row.enrollment_status, total_units=total
    )


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


async def get_pending_overload_units(
    db: AsyncSession, *, student_id: uuid.UUID, academic_term_id: uuid.UUID
) -> Decimal:
    return await repository.get_pending_overload_units(
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


async def count_section_enrollments(db: AsyncSession, section_id: uuid.UUID) -> int:
    return await repository.count_section_enrollments(db, section_id)


async def get_enrollment_report_counts(
    db: AsyncSession,
    *,
    term_id: uuid.UUID,
    group_by: str,
    program_code: str | None = None,
    status: str | None = None,
    department: str | None = None,
    college: str | None = None,
) -> list[tuple[str, int]]:
    """Return aggregated enrollment counts grouped by the selected field."""
    return await repository.get_enrollment_report_counts(
        db,
        term_id=term_id,
        group_by=group_by,
        program_code=program_code,
        status=status,
        department=department,
        college=college,
    )


async def list_available_courses(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    term: AcademicTermRead,
) -> list[AvailableCourseRead]:
    profile = await users.get_student_profile(db, student_id)
    if profile is None:
        raise ResourceNotFoundError

    course_rows = await curriculum.active_curriculum_courses(
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
    verdicts = await academic.preview_academics(
        db, student_id=student_id, term=term, section_ids=section_ids
    )
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
        verdict = verdicts.get(section.id)
        if course is None or verdict is None:
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
                prerequisites_satisfied=verdict.prerequisites_satisfied,
                block_reason=verdict.block_reason,
                blocks=verdict.blocks,
                waiver_available=verdict.waiver_available,
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

            course_rows = await curriculum.active_curriculum_courses(
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
            selected_course_ids = [section.course_id for section in selected]
            if len(selected_course_ids) != len(set(selected_course_ids)):
                raise ConflictError(
                    "Cannot enroll in multiple sections of the same course.",
                    code="ENROLL_DUPLICATE_COURSE",
                )

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
                    currency=financial_hold.currency,
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
                        ),
                        is_back_subject=courses_by_id[
                            section.course_id
                        ].is_back_subject,
                    )
                    for section in selected
                ],
            )

            check_schedule_conflict(
                ConflictStudent(student_id=student_id),
                ConflictTerm(academic_term_id=term.id),
                _selected_conflict_sections(term, selected, courses_by_id),
            )

            verdict = await academic.evaluate_academics(
                db, student_id=student_id, term=term, section_ids=section_ids
            )
            academic.require_academics(verdict)

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
            await sections.mark_seats_taken(db, section_ids)

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
            try:
                await billing.stage_assessment(
                    db,
                    student_id=student_id,
                    term_id=term.id,
                    courses=[
                        billing.AssessmentCourse(
                            section_id=section.id,
                            units=(
                                courses_by_id[section.course_id].lecture_units
                                + courses_by_id[section.course_id].lab_units
                            ),
                        )
                        for section in sorted(selected, key=lambda row: str(row.id))
                    ],
                    due_date=period_end.date(),
                )
            except billing.AssessmentInvalid:
                raise EnrollmentAssessmentUnavailableError from None

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


def assess_overload_eligibility(
    *,
    total_units_earned: Decimal,
    total_units_required: Decimal,
    gwa: Decimal | None,
    stored_graduating: bool = False,
) -> OverloadEligibilityRead:
    """Apply the 85%-completion or 1.75-GWA overload eligibility rule."""
    completion_ratio = (
        total_units_earned / total_units_required
        if total_units_required > 0
        else Decimal("0")
    )
    is_graduating = stored_graduating or completion_ratio >= OVERLOAD_GRADUATING_RATIO
    eligible = is_graduating or bool(gwa is not None and gwa <= OVERLOAD_REQUIRED_GWA)
    return OverloadEligibilityRead(
        eligible=eligible,
        is_graduating=is_graduating,
        gwa=gwa,
        required_gwa=OVERLOAD_REQUIRED_GWA,
    )


async def file_overload_request(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    term: AcademicTermRead,
    requested_units: int,
    justification: str,
    role: str = "student",
    client_ip: str | None = None,
) -> OverloadRequestFiledRead:
    """Validate and file one overload request with its audit row atomically."""
    if Decimal(requested_units) > MAX_OVERLOAD_UNITS:
        raise OverloadCapExceededError(maximum_overload_units=MAX_OVERLOAD_UNITS)

    profile = await users.get_student_profile(db, student_id)
    if profile is None:
        raise ResourceNotFoundError
    program = await curriculum.get_program(db, profile.program_id)
    if program is None:
        raise ResourceNotFoundError
    transcript = await grading.read_transcript(
        db, student_id=student_id, complete=True, limit=100
    )
    gwa = await grading.get_previous_regular_term_gwa(
        db, student_id=student_id, before=term.start_date
    )
    eligibility = assess_overload_eligibility(
        total_units_earned=transcript.total_units_earned,
        total_units_required=program.total_units_required,
        gwa=gwa,
        stored_graduating=profile.is_graduating_senior,
    )
    if not eligibility.eligible:
        raise OverloadNotEligibleError(
            is_graduating=eligibility.is_graduating,
            gwa=eligibility.gwa,
            required_gwa=eligibility.required_gwa,
        )

    try:
        request_row = repository.add_overload_request(
            db,
            student_id=student_id,
            academic_term_id=term.id,
            requested_units=requested_units,
            justification=justification,
            state=OVERLOAD_PENDING,
        )
        await db.flush()
        occurred_at = datetime.now(UTC)
        audit.stage(
            db,
            action=OVERLOAD_REQUEST_FILED_ACTION,
            actor_id=student_id,
            target_type="overload_request",
            target_id=request_row.id,
            occurred_at=occurred_at,
            prior_state={},
            new_state={
                "state": OVERLOAD_PENDING,
                "requestedUnits": requested_units,
                "academicTermId": str(term.id),
            },
            payload={
                "role": role,
                "timestamp": occurred_at.isoformat(),
                "ip_address": client_ip,
            },
        )
        await db.commit()
        return OverloadRequestFiledRead(
            overload_request_id=request_row.id, state=OVERLOAD_PENDING
        )
    except Exception:
        await db.rollback()
        raise


async def _overload_request_reads(db: AsyncSession, request_rows) -> list:
    identities = await users.overload_student_identities(
        db, {row.student_id for row in request_rows}
    )
    programs = await curriculum.programs_by_ids(
        db, {identity.program_id for identity in identities.values()}
    )
    terms = {
        term.id: term
        for term in await academics.record_terms(
            db, {row.academic_term_id for row in request_rows}
        )
    }
    reads = []
    for request_row in request_rows:
        identity = identities.get(request_row.student_id)
        term = terms.get(request_row.academic_term_id)
        program = programs.get(identity.program_id) if identity is not None else None
        if identity is None or term is None or program is None:
            continue
        reads.append(
            OverloadRequestRead(
                overload_request_id=request_row.id,
                student_id=request_row.student_id,
                student_number=identity.student_number,
                student_name=identity.full_name,
                program_code=program.code,
                department=program.department,
                college=program.college,
                term=f"{term.academic_year}-{term.semester}",
                requested_units=request_row.requested_units,
                justification=request_row.justification,
                state=request_row.state,
                reviewed_by=request_row.reviewed_by,
                remarks=request_row.review_remarks,
                created_at=request_row.created_at,
                reviewed_at=request_row.reviewed_at,
            )
        )
    return reads


def _overload_in_scope(
    row: OverloadRequestRead,
    *,
    position: str,
    department: str | None,
    college: str | None,
) -> bool:
    return (
        position == "registrar"
        or (position == "chairperson" and department == row.department)
        or (position == "dean" and college is not None and college == row.college)
    )


async def list_overload_requests(
    db: AsyncSession,
    *,
    position: str,
    department: str | None,
    college: str | None,
) -> list[OverloadRequestRead]:
    """Return only overload requests inside the Admin caller's role scope."""
    if position not in {"chairperson", "dean", "registrar"}:
        raise ForbiddenError("This Admin position cannot review overload requests.")
    reads = await _overload_request_reads(
        db, await repository.list_overload_requests(db)
    )
    return [
        row
        for row in reads
        if _overload_in_scope(
            row,
            position=position,
            department=department,
            college=college,
        )
    ]


async def decide_overload_request(
    db: AsyncSession,
    *,
    overload_request_id: uuid.UUID,
    decision: str,
    remarks: str | None,
    reviewer_id: uuid.UUID,
    position: str,
    department: str | None,
    college: str | None,
    client_ip: str | None = None,
) -> OverloadRequestRead:
    """Apply one in-scope Dean decision and notify the student atomically."""
    if position != "dean":
        raise ForbiddenError("Only a College Dean may decide an overload request.")
    request_row = await repository.get_overload_request_for_update(
        db,
        overload_request_id=overload_request_id,
    )
    if request_row is None:
        raise ResourceNotFoundError
    reads = await _overload_request_reads(db, [request_row])
    if not reads or not _overload_in_scope(
        reads[0],
        position=position,
        department=department,
        college=college,
    ):
        raise ResourceNotFoundError
    if request_row.state != OVERLOAD_PENDING:
        raise RequestAlreadyDecidedError
    normalized_remarks = remarks.strip() if remarks is not None else None
    if decision == "REJECT" and not normalized_remarks:
        raise DecisionRemarksRequiredError

    new_state = OVERLOAD_APPROVED if decision == "APPROVE" else OVERLOAD_REJECTED
    occurred_at = datetime.now(UTC)
    prior = {
        "state": request_row.state,
        "reviewedBy": None,
        "remarks": None,
    }
    request_row.state = new_state
    request_row.reviewed_by = reviewer_id
    request_row.review_remarks = normalized_remarks
    request_row.reviewed_at = occurred_at
    audit.stage(
        db,
        action=f"enrollment.overload_{new_state.lower()}",
        actor_id=reviewer_id,
        target_type="overload_request",
        target_id=request_row.id,
        occurred_at=occurred_at,
        prior_state=prior,
        new_state={
            "state": new_state,
            "reviewedBy": str(reviewer_id),
            "remarks": normalized_remarks,
        },
        payload={
            "role": position,
            "timestamp": occurred_at.isoformat(),
            "ip_address": client_ip,
        },
    )
    publish_transactional(
        db,
        Event(
            name=OVERLOAD_REQUEST_DECIDED_EVENT,
            payload={
                "user_id": str(request_row.student_id),
                "overload_request_id": str(request_row.id),
                "state": new_state,
                "decision": decision,
                "remarks": normalized_remarks,
            },
        ),
    )
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    refreshed = await repository.get_overload_request(db, overload_request_id)
    if refreshed is None:
        raise ResourceNotFoundError
    [result] = await _overload_request_reads(db, [refreshed])
    return result


async def _add_drop_course_reads(
    db: AsyncSession, items, *, request_state: str
) -> list[AddDropCourseRead]:
    if not items:
        return []
    section_ids = [item.section_id for item in items]
    section_rows = await sections.schedule_sections(db, section_ids)
    sections_by_id = {row.id: row for row in section_rows}
    course_rows = await curriculum.schedule_courses(
        db, [section.course_id for section in section_rows]
    )
    courses_by_id = {course.id: course for course in course_rows}

    reads: list[AddDropCourseRead] = []
    for item in items:
        section = sections_by_id.get(item.section_id)
        if section is None:
            continue
        course = courses_by_id.get(section.course_id)
        if course is None:
            continue
        tag = ADD_DROP_COURSE_TAGS[(request_state, item.action)]
        reads.append(
            AddDropCourseRead(
                section_id=item.section_id,
                course_code=course.course_code,
                course_title=course.title,
                units=course.lecture_units + course.lab_units,
                tag=tag,
                reason=item.reason,
            )
        )
    return reads


async def _student_add_drop_request_reads(
    db: AsyncSession, requests
) -> list[AddDropRequestRead]:
    reads: list[AddDropRequestRead] = []
    for request_row in requests:
        course_reads = await _add_drop_course_reads(
            db, request_row.items, request_state=request_row.state
        )
        reads.append(
            AddDropRequestRead(
                request_id=request_row.id,
                student_id=request_row.student_id,
                academic_term_id=request_row.academic_term_id,
                state=request_row.state,
                units_before=request_row.units_before or Decimal("0"),
                units_after=request_row.units_after or Decimal("0"),
                comments=request_row.comments,
                remarks=request_row.remarks,
                version=request_row.version,
                courses=course_reads,
                created_at=request_row.created_at,
                reviewed_at=request_row.reviewed_at,
                reviewed_by=request_row.reviewed_by,
            )
        )
    return reads


async def _admin_add_drop_request_reads(
    db: AsyncSession, requests
) -> list[AdminAddDropRequestRead]:
    if not requests:
        return []
    identities = await users.overload_student_identities(
        db, {row.student_id for row in requests}
    )
    programs = await curriculum.programs_by_ids(
        db, {identity.program_id for identity in identities.values()}
    )
    terms = {
        term.id: term
        for term in await academics.record_terms(
            db,
            {
                row.academic_term_id
                for row in requests
                if row.academic_term_id is not None
            },
        )
    }
    reads: list[AdminAddDropRequestRead] = []
    for request_row in requests:
        identity = identities.get(request_row.student_id)
        term = terms.get(request_row.academic_term_id)
        program = programs.get(identity.program_id) if identity is not None else None
        if identity is None or term is None or program is None:
            continue
        course_reads = await _add_drop_course_reads(
            db, request_row.items, request_state=request_row.state
        )
        reads.append(
            AdminAddDropRequestRead(
                request_id=request_row.id,
                student_id=request_row.student_id,
                student_number=identity.student_number,
                student_name=identity.full_name,
                program_code=program.code,
                department=program.department,
                college=program.college,
                term=f"{term.academic_year}-{term.semester}",
                academic_term_id=request_row.academic_term_id,
                state=request_row.state,
                units_before=request_row.units_before or Decimal("0"),
                units_after=request_row.units_after or Decimal("0"),
                comments=request_row.comments,
                remarks=request_row.remarks,
                version=request_row.version,
                courses=course_reads,
                created_at=request_row.created_at,
                reviewed_at=request_row.reviewed_at,
                reviewed_by=request_row.reviewed_by,
            )
        )
    return reads


def _add_drop_in_scope(
    row: AdminAddDropRequestRead,
    *,
    position: str,
    department: str | None,
    college: str | None,
) -> bool:
    return (
        position == "registrar"
        or (position == "chairperson" and department == row.department)
        or (position == "dean" and college is not None and college == row.college)
    )


async def file_add_drop_request(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    request_data: AddDropRequestCreate,
    client_ip: str | None = None,
    as_of: datetime | None = None,
) -> AddDropRequestRead:
    if not request_data.add and not request_data.drop:
        raise ValidationError("At least one course section must be added or dropped.")

    add_ids = [item.section_id for item in request_data.add]
    drop_ids = [item.section_id for item in request_data.drop]
    if len(add_ids) != len(set(add_ids)) or len(drop_ids) != len(set(drop_ids)):
        raise ValidationError("Duplicate sections in request.")
    if set(add_ids) & set(drop_ids):
        raise ValidationError("Cannot both add and drop the same section.")

    term_enrollment = await repository.get_latest_term_enrollment(db, student_id)
    if term_enrollment is None:
        raise ResourceNotFoundError
    term = await academics.get_academic_term(db, term_enrollment.academic_term_id)
    if term is None:
        raise ResourceNotFoundError

    period_start = term.regular_enrollment_start or term.early_enrollment_start
    period_end = term.add_drop_deadline
    if period_start is None or period_end is None:
        raise ResourceNotFoundError
    start_utc = _as_utc(period_start)
    end_utc = _as_utc(period_end)
    now = _as_utc(as_of or datetime.now(UTC))
    if not (start_utc <= now <= end_utc):
        raise AddDropPeriodClosedError(
            period_start=start_utc.isoformat(),
            period_end=end_utc.isoformat(),
        )

    pending = await repository.get_student_pending_add_drop_request(
        db, student_id, term.id
    )
    if pending is not None:
        raise ConflictError(
            "There is already a pending add/drop request for this term.",
            code="REQUEST_ALREADY_PENDING",
        )

    profile = await users.get_student_profile(db, student_id)
    if profile is None:
        raise ResourceNotFoundError

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
            currency=financial_hold.currency,
        )
    active_hold = next((hold for hold in holds if hold.is_blocking), None)
    if active_hold is not None:
        raise EnrollmentHoldActiveError(
            office=active_hold.office, hold_type=active_hold.type
        )

    standing = await grading.get_student_scholastic_standing(db, student_id)
    if standing.value.upper() in BLOCKED_ACADEMIC_STANDINGS:
        raise EnrollmentStandingBlockedError(standing=standing.value)

    enrolled = await repository.enrolled_sections(db, student_id, term.id)
    enrolled_section_ids = {row.section_id for row in enrolled}

    if not set(drop_ids).issubset(enrolled_section_ids):
        raise ValidationError("Cannot drop a course you are not enrolled in.")

    all_needed_ids = list(enrolled_section_ids | set(add_ids))
    all_schedule_sections = await sections.schedule_sections(db, all_needed_ids)
    sections_by_id = {s.id: s for s in all_schedule_sections}
    if any(sid not in sections_by_id for sid in all_needed_ids):
        raise ResourceNotFoundError

    course_rows = await curriculum.schedule_courses(
        db, [s.course_id for s in all_schedule_sections]
    )
    courses_by_id = {c.id: c for c in course_rows}
    if any(s.course_id not in courses_by_id for s in all_schedule_sections):
        raise ResourceNotFoundError

    if add_ids and profile.curriculum_id:
        curriculum_course_ids = await curriculum.list_curriculum_course_ids(
            db, profile.curriculum_id
        )
        if any(
            sections_by_id[sid].course_id not in curriculum_course_ids
            for sid in add_ids
        ):
            raise ResourceNotFoundError

    resulting_section_ids = (enrolled_section_ids - set(drop_ids)) | set(add_ids)
    resulting_course_ids = [
        sections_by_id[sid].course_id for sid in resulting_section_ids
    ]
    if len(resulting_course_ids) != len(set(resulting_course_ids)):
        raise ConflictError(
            "Cannot enroll in multiple sections of the same course.",
            code="ENROLL_DUPLICATE_COURSE",
        )

    section_units = {
        s.id: (
            courses_by_id[s.course_id].lecture_units
            + courses_by_id[s.course_id].lab_units
        )
        for s in all_schedule_sections
    }

    units_before = sum(
        (section_units[sid] for sid in enrolled_section_ids), Decimal("0")
    )
    units_dropped = sum((section_units[sid] for sid in drop_ids), Decimal("0"))
    units_added = sum((section_units[sid] for sid in add_ids), Decimal("0"))
    units_after = units_before - units_dropped + units_added

    minimum_load = getattr(
        settings, "enrollment_minimum_regular_load", MINIMUM_REGULAR_LOAD
    )
    if units_after < minimum_load:
        raise BelowMinimumLoadError(
            minimum_load=minimum_load,
            units_after=units_after,
        )

    if add_ids:
        for sid in add_ids:
            sec = sections_by_id[sid]
            if sec.academic_term_id != term.id or sec.status.lower() != "open":
                raise ResourceNotFoundError

        seat_counts = await repository.count_active_section_enrollments(db, add_ids)
        for sid in sorted(add_ids, key=str):
            sec = sections_by_id[sid]
            occupied = seat_counts.get(sid, 0)
            if occupied >= sec.capacity:
                raise EnrollmentSectionFullError(section_id=sid)

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
            [CeilingSection(units=section_units[sid]) for sid in resulting_section_ids],
        )

        resulting_sections = [sections_by_id[sid] for sid in resulting_section_ids]
        check_schedule_conflict(
            ConflictStudent(student_id=student_id),
            ConflictTerm(academic_term_id=term.id),
            _selected_conflict_sections(term, resulting_sections, courses_by_id),
        )

        academic.require_academics(
            await academic.evaluate_changes(
                db,
                student_id=student_id,
                term=term,
                add_section_ids=add_ids,
                drop_section_ids=drop_ids,
            )
        )

    drop_tuples = [(item.section_id, item.reason) for item in request_data.drop]
    request_row = repository.add_add_drop_request(
        db,
        student_id=student_id,
        academic_term_id=term.id,
        comments=request_data.comments,
        units_before=units_before,
        units_after=units_after,
        add_section_ids=add_ids,
        drop_sections=drop_tuples,
    )
    await db.flush()

    occurred_at = datetime.now(UTC)
    audit.stage(
        db,
        action=ADD_DROP_REQUEST_FILED_ACTION,
        actor_id=student_id,
        target_type="add_drop_request",
        target_id=request_row.id,
        occurred_at=occurred_at,
        prior_state=None,
        new_state={
            "state": ADD_DROP_PENDING,
            "add_count": len(add_ids),
            "drop_count": len(drop_ids),
        },
        payload={
            "role": "student",
            "timestamp": occurred_at.isoformat(),
            "ip_address": client_ip,
        },
    )
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    refreshed = await repository.get_add_drop_request(db, request_row.id)
    if refreshed is None:
        raise ResourceNotFoundError
    [result] = await _student_add_drop_request_reads(db, [refreshed])
    return result


async def get_student_add_drop_request(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    request_id: uuid.UUID,
) -> AddDropRequestRead:
    request_row = await repository.get_add_drop_request(db, request_id)
    if request_row is None or request_row.student_id != student_id:
        raise ResourceNotFoundError
    [result] = await _student_add_drop_request_reads(db, [request_row])
    return result


async def list_student_add_drop_requests(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
) -> list[AddDropRequestRead]:
    requests = await repository.list_student_add_drop_requests(db, student_id)
    return await _student_add_drop_request_reads(db, requests)


async def list_admin_add_drop_requests(
    db: AsyncSession,
    *,
    position: str,
    department: str | None,
    college: str | None,
) -> list[AdminAddDropRequestRead]:
    if position not in {"chairperson", "dean", "registrar"}:
        raise ForbiddenError("This Admin position cannot review add/drop requests.")
    requests = await repository.list_all_add_drop_requests(db)
    reads = await _admin_add_drop_request_reads(db, requests)
    return [
        r
        for r in reads
        if _add_drop_in_scope(
            r, position=position, department=department, college=college
        )
    ]


async def decide_add_drop_request(
    db: AsyncSession,
    *,
    request_id: uuid.UUID,
    decision: str,
    remarks: str | None,
    reviewer_id: uuid.UUID,
    position: str,
    department: str | None,
    college: str | None,
    client_ip: str | None = None,
    version: int,
) -> AdminAddDropRequestRead:
    """Decide one request, retrying an approval that lost a snapshot race."""
    if position not in {"chairperson", "dean"}:
        raise ForbiddenError(
            "Only a Chairperson or College Dean may decide an add/drop request."
        )

    for attempt in range(ADD_DROP_DECISION_ATTEMPTS):
        try:
            return await _decide_add_drop_request(
                db,
                request_id=request_id,
                decision=decision,
                remarks=remarks,
                reviewer_id=reviewer_id,
                position=position,
                department=department,
                college=college,
                client_ip=client_ip,
                version=version,
            )
        except DBAPIError as exc:
            await db.rollback()
            if getattr(exc.orig, "sqlstate", None) not in {"40001", "40P01"}:
                raise
            if attempt == ADD_DROP_DECISION_ATTEMPTS - 1:
                raise ConflictError(
                    "Request concurrency conflict. Please refresh and try again.",
                    code="REQUEST_CONCURRENCY_CONFLICT",
                ) from None
    raise AssertionError("unreachable")


async def _decide_add_drop_request(
    db: AsyncSession,
    *,
    request_id: uuid.UUID,
    decision: str,
    remarks: str | None,
    reviewer_id: uuid.UUID,
    position: str,
    department: str | None,
    college: str | None,
    client_ip: str | None,
    version: int,
) -> AdminAddDropRequestRead:
    if decision == "APPROVE":
        # Approval reissues the COR, which needs one snapshot for the whole
        # transaction. Finish the authentication reads first, as issuance does.
        await db.commit()
        await repository.begin_certificate_snapshot(db)

    request_row = await repository.get_add_drop_request_for_update(db, request_id)
    if request_row is None:
        raise ResourceNotFoundError

    reads = await _admin_add_drop_request_reads(db, [request_row])
    if not reads or not _add_drop_in_scope(
        reads[0],
        position=position,
        department=department,
        college=college,
    ):
        raise ResourceNotFoundError

    if request_row.state != ADD_DROP_PENDING:
        raise RequestAlreadyDecidedError

    if request_row.version != version:
        raise ConflictError(
            "Request concurrency conflict. Please refresh and try again.",
            code="REQUEST_CONCURRENCY_CONFLICT",
        )

    normalized_remarks = remarks.strip() if remarks is not None else None
    if decision == "REJECT" and not normalized_remarks:
        raise DecisionRemarksRequiredError

    new_state = ADD_DROP_APPROVED if decision == "APPROVE" else ADD_DROP_REJECTED
    occurred_at = datetime.now(UTC)

    try:
        if decision == "APPROVE":
            add_ids = [
                item.section_id for item in request_row.items if item.action == "ADD"
            ]
            drop_ids = [
                item.section_id for item in request_row.items if item.action == "DROP"
            ]

            if add_ids:
                locked_sections = await sections.lock_for_enrollment(db, add_ids)
                locked_by_id = {s.id: s for s in locked_sections}
                if set(locked_by_id) != set(add_ids) or any(
                    s.status.lower() != "open" for s in locked_sections
                ):
                    raise ResourceNotFoundError

                seat_counts = await repository.count_active_section_enrollments(
                    db, add_ids
                )
                for sid in sorted(add_ids, key=str):
                    if seat_counts.get(sid, 0) >= locked_by_id[sid].capacity:
                        raise EnrollmentSectionFullError(section_id=sid)
                await sections.mark_seats_taken(db, add_ids)

            term_enrollment = await repository.enrollment(
                db, request_row.student_id, request_row.academic_term_id, lock=True
            )
            if term_enrollment is None:
                raise ResourceNotFoundError

            current_enrolled = await repository.enrolled_sections(
                db, request_row.student_id, request_row.academic_term_id
            )
            current_enrolled_ids = {row.section_id for row in current_enrolled}

            if not set(drop_ids).issubset(current_enrolled_ids):
                raise ConflictError(
                    "Cannot drop a course you are no longer enrolled in."
                )

            resulting_section_ids = (current_enrolled_ids - set(drop_ids)) | set(
                add_ids
            )
            all_needed_ids = list(current_enrolled_ids | set(add_ids))
            all_schedule_sections = await sections.schedule_sections(db, all_needed_ids)
            sections_by_id = {s.id: s for s in all_schedule_sections}
            course_rows = await curriculum.schedule_courses(
                db, [s.course_id for s in all_schedule_sections]
            )
            courses_by_id = {c.id: c for c in course_rows}

            resulting_course_ids = [
                sections_by_id[sid].course_id for sid in resulting_section_ids
            ]
            if len(resulting_course_ids) != len(set(resulting_course_ids)):
                raise ConflictError(
                    "Cannot enroll in multiple sections of the same course.",
                    code="ENROLL_DUPLICATE_COURSE",
                )

            section_units = {
                s.id: (
                    courses_by_id[s.course_id].lecture_units
                    + courses_by_id[s.course_id].lab_units
                )
                for s in all_schedule_sections
            }
            units_after = sum(
                (section_units[sid] for sid in resulting_section_ids), Decimal("0")
            )
            minimum_load = getattr(
                settings, "enrollment_minimum_regular_load", MINIMUM_REGULAR_LOAD
            )
            if units_after < minimum_load:
                raise BelowMinimumLoadError(
                    minimum_load=minimum_load,
                    units_after=units_after,
                )

            profile = await users.get_student_profile(db, request_row.student_id)
            term = await academics.get_academic_term(db, request_row.academic_term_id)
            if profile is None or term is None:
                raise ResourceNotFoundError

            if add_ids:
                prescribed_load = await curriculum.get_prescribed_load(
                    db,
                    curriculum_id=profile.curriculum_id,
                    year_level=profile.year_level,
                    semester=term.semester,
                )
                approved_overload = await repository.get_approved_overload_units(
                    db, student_id=request_row.student_id, academic_term_id=term.id
                )
                standing = await grading.get_student_scholastic_standing(
                    db, request_row.student_id
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
                        CeilingSection(units=section_units[sid])
                        for sid in resulting_section_ids
                    ],
                )

                resulting_sections = [
                    sections_by_id[sid] for sid in resulting_section_ids
                ]
                check_schedule_conflict(
                    ConflictStudent(student_id=request_row.student_id),
                    ConflictTerm(academic_term_id=term.id),
                    _selected_conflict_sections(
                        term, resulting_sections, courses_by_id
                    ),
                )

                academic.require_academics(
                    await academic.evaluate_changes(
                        db,
                        student_id=request_row.student_id,
                        term=term,
                        add_section_ids=add_ids,
                        drop_section_ids=drop_ids,
                    )
                )

            for item in request_row.items:
                if item.action == "DROP":
                    sce = await repository.get_student_course_enrollment(
                        db, term_enrollment.id, item.section_id
                    )
                    if sce is not None:
                        sce.status = "dropped"
                elif item.action == "ADD":
                    sce = await repository.get_student_course_enrollment(
                        db, term_enrollment.id, item.section_id
                    )
                    if sce is not None:
                        sce.status = "enrolled"
                    else:
                        repository.add_course_enrollments(
                            db,
                            term_enrollment_id=term_enrollment.id,
                            academic_term_id=request_row.academic_term_id,
                            section_ids=[item.section_id],
                        )

            await db.flush()
            try:
                assessment = await billing.stage_assessment(
                    db,
                    student_id=request_row.student_id,
                    term_id=term.id,
                    courses=[
                        billing.AssessmentCourse(
                            section_id=sid, units=section_units[sid]
                        )
                        for sid in sorted(resulting_section_ids, key=str)
                    ],
                    due_date=_as_utc(term.add_drop_deadline).date(),
                )
            except billing.AssessmentInvalid:
                raise CorNotFinal(["NO_ASSESSMENT"]) from None
            # A student who has not issued a COR yet has nothing to supersede.
            if await repository.current(db, term_enrollment.id, lock=True):
                await stage_reissue(
                    db,
                    student_id=request_row.student_id,
                    term_id=term.id,
                    actor_id=reviewer_id,
                    assessment=assessment,
                    client_ip=client_ip,
                    actor_role=position,
                )

        prior = {
            "state": request_row.state,
            "reviewedBy": None,
            "remarks": None,
        }
        request_row.state = new_state
        request_row.reviewed_by = reviewer_id
        request_row.remarks = normalized_remarks
        request_row.reviewed_at = occurred_at
        request_row.version += 1

        audit.stage(
            db,
            action=ADD_DROP_REQUEST_DECIDED_ACTION,
            actor_id=reviewer_id,
            target_type="add_drop_request",
            target_id=request_row.id,
            occurred_at=occurred_at,
            prior_state=prior,
            new_state={
                "state": new_state,
                "reviewedBy": str(reviewer_id),
                "remarks": normalized_remarks,
            },
            payload={
                "role": position,
                "position": position,
                "remarks": normalized_remarks,
                "timestamp": occurred_at.isoformat(),
                "ip_address": client_ip,
            },
        )

        publish_transactional(
            db,
            Event(
                name=ADD_DROP_REQUEST_DECIDED_EVENT,
                payload={
                    "user_id": str(request_row.student_id),
                    "request_id": str(request_row.id),
                    "state": new_state,
                    "decision": decision,
                    "remarks": normalized_remarks,
                },
            ),
        )

        await db.commit()
    except Exception:
        await db.rollback()
        raise

    refreshed = await repository.get_add_drop_request(db, request_id)
    if refreshed is None:
        raise ResourceNotFoundError
    [result] = await _admin_add_drop_request_reads(db, [refreshed])
    return result
