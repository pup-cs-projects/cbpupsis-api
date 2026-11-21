"""Schedule composition and export transaction boundaries."""

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.schedule import exports, repository
from cbpupsis_api_student.domains.schedule.constants import (
    EXPORT_TIMEOUT_SECONDS,
    MAX_COURSES,
    MAX_MEETINGS,
    MAX_TERM_DAYS,
)
from cbpupsis_api_student.domains.schedule.exceptions import ScheduleUnavailableError
from cbpupsis_api_student.domains.schedule.schemas import (
    ScheduleCourse,
    ScheduleMeeting,
    ScheduleRead,
    ScheduleTerm,
)
from cbpupsis_core.exceptions import ValidationError
from cbpupsis_shared.domains.academics import client as academics
from cbpupsis_shared.domains.academics.schemas import TermRead
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.curriculum import client as curriculum
from cbpupsis_shared.domains.enrollment import client as enrollment
from cbpupsis_shared.domains.sections import client as sections
from cbpupsis_shared.domains.users import client as users

logger = logging.getLogger(__name__)


def validate_selector(year: str | None, semester: str | None) -> None:
    if (year is None) != (semester is None):
        raise ValidationError("Supply semester and year together.")
    if (year is not None and not year.strip()) or (
        semester is not None and not semester.strip()
    ):
        raise ValidationError("Semester and year cannot be blank.")


async def _identity(
    db: AsyncSession, student_id: uuid.UUID, student_number: str | None
):
    identity = await users.schedule_identity(db, student_id)
    if identity is None or (
        student_number is not None and student_number != identity.student_number
    ):
        raise ResourceNotFoundError
    return identity


async def _compose(
    db: AsyncSession, student_id: uuid.UUID, identity, term: TermRead | None, q: str
) -> ScheduleRead:
    result = ScheduleRead(
        student_name=identity.full_name,
        student_number=identity.student_number,
        term=None,
        state="termUnavailable",
        message="No academic term is configured for this selection.",
    )
    if term is None:
        return result
    if not 0 <= (term.end_date - term.start_date).days <= MAX_TERM_DAYS:
        raise ScheduleUnavailableError
    result.term = ScheduleTerm(
        id=term.id,
        year=term.academic_year,
        semester=term.semester,
        start_date=term.start_date,
        end_date=term.end_date,
    )
    enrolled = await enrollment.enrolled_sections(db, student_id, term.id)
    if len(enrolled) > MAX_COURSES:
        raise ScheduleUnavailableError
    section_rows = await sections.schedule_sections(
        db, [row.section_id for row in enrolled]
    )
    course_rows = {
        row.id: row
        for row in await curriculum.schedule_courses(
            db, [row.course_id for row in section_rows]
        )
    }
    names = await users.schedule_names(
        db, [row.faculty_id for row in section_rows if row.faculty_id]
    )
    if (
        len(section_rows) != len(enrolled)
        or sum(len(row.meetings) for row in section_rows) > MAX_MEETINGS
    ):
        raise ScheduleUnavailableError
    statuses = {row.section_id: row.status for row in enrolled}
    courses = []
    for section in section_rows:
        course = course_rows.get(section.course_id)
        if course is None or section.academic_term_id != term.id:
            raise ScheduleUnavailableError
        meetings = []
        hours = {"lecture": Decimal(0), "laboratory": Decimal(0)}
        for meeting in section.meetings:
            kind = (
                "laboratory"
                if meeting.schedule_type in {"lab", "laboratory"}
                else meeting.schedule_type
            )
            if (
                kind not in hours
                or not 0 <= meeting.day_of_week <= 6
                or meeting.end_time <= meeting.start_time
            ):
                raise ScheduleUnavailableError
            duration = datetime.combine(date.min, meeting.end_time) - datetime.combine(
                date.min, meeting.start_time
            )
            hours[kind] += Decimal(str(duration.total_seconds())) / Decimal(3600)
            meetings.append(
                ScheduleMeeting(
                    id=meeting.id,
                    day_of_week=meeting.day_of_week,
                    start_time=meeting.start_time,
                    end_time=meeting.end_time,
                    room=meeting.room,
                    type=kind,
                )
            )
        courses.append(
            ScheduleCourse(
                code=course.course_code,
                title=course.title,
                section=section.section_name,
                section_id=section.id,
                room=", ".join(
                    sorted({meeting.room for meeting in meetings if meeting.room})
                )
                or None,
                faculty_name=names.get(section.faculty_id),
                units=course.lecture_units + course.lab_units,
                lecture_hours=hours["lecture"],
                lab_hours=hours["laboratory"],
                status=statuses[section.id],
                meetings=meetings,
            )
        )
    needle = q.strip().casefold()
    result.courses = sorted(
        [
            course
            for course in courses
            if not needle
            or any(
                needle in value.casefold()
                for value in (course.code, course.title, course.faculty_name or "")
            )
        ],
        key=lambda course: (course.code, course.section, str(course.section_id)),
    )
    result.total_credits = sum((course.units for course in result.courses), Decimal(0))
    result.total_lecture_hours = sum(
        (course.lecture_hours for course in result.courses), Decimal(0)
    )
    result.total_lab_hours = sum(
        (course.lab_hours for course in result.courses), Decimal(0)
    )
    result.state = (
        "ready" if result.courses else "noMatches" if courses else "notEnrolled"
    )
    result.message = (
        None
        if result.courses
        else "No courses match this filter."
        if courses
        else "You are not enrolled in any courses for this term."
    )
    return result


async def read_schedule(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    year: str | None = None,
    semester: str | None = None,
    q: str = "",
    student_number: str | None = None,
) -> ScheduleRead:
    validate_selector(year, semester)
    await db.commit()
    await repository.begin_snapshot(db)
    identity = await _identity(db, student_id, student_number)
    term = await academics.resolve_term(db, year, semester)
    return await _compose(db, student_id, identity, term, q)


async def calendar_snapshot(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    year: str | None = None,
    semester: str | None = None,
):
    """Return an unfiltered coherent timetable for a calendar integration."""
    try:
        schedule = await read_schedule(
            db, student_id=student_id, year=year, semester=semester
        )
        if schedule.term is None:
            raise ScheduleUnavailableError
        holidays = await academics.holidays(db, schedule.term.id)
        return schedule.term, exports.calendar_events(schedule, student_id, holidays)
    finally:
        await db.rollback()


async def export_schedule(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    format: str,
    year: str | None = None,
    semester: str | None = None,
) -> bytes:
    validate_selector(year, semester)
    if format not in {"pdf", "ics"}:
        raise ValidationError("Format must be pdf or ics.")
    try:
        async with asyncio.timeout(EXPORT_TIMEOUT_SECONDS):
            # Auth dependencies finish before the read snapshot is established.
            await db.commit()
            for attempt in range(3):
                try:
                    return await _export_once(db, student_id, format, year, semester)
                except DBAPIError as exc:
                    await db.rollback()
                    if getattr(exc.orig, "sqlstate", None) != "40001" or attempt == 2:
                        raise
            raise ScheduleUnavailableError
    except ResourceNotFoundError:
        await db.rollback()
        raise
    except asyncio.CancelledError:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        # No exception text: drivers and renderers can include private row data.
        logger.warning(
            "schedule.export_failed", extra={"failure_type": type(exc).__name__}
        )
        raise ScheduleUnavailableError from None


async def _export_once(
    db: AsyncSession,
    student_id: uuid.UUID,
    format: str,
    year: str | None,
    semester: str | None,
) -> bytes:
    await repository.begin_snapshot(db)
    identity = await _identity(db, student_id, None)
    term = await academics.resolve_term(db, year, semester)
    state = (
        await repository.lock_export_state(db, student_id, term.id)
        if format == "ics" and term
        else None
    )
    schedule = await _compose(db, student_id, identity, term, "")
    if format == "pdf":
        content = await asyncio.to_thread(exports.render_pdf, schedule)
        await db.rollback()
        return content
    if term is None:
        content = await asyncio.to_thread(
            exports.render_calendar, {}, 0, datetime.now(UTC)
        )
        await db.rollback()
        return content
    holidays = await academics.holidays(db, term.id)
    canonical = json.dumps(
        {
            "schedule": schedule.model_dump(mode="json"),
            "holidays": sorted(
                (holiday.model_dump(mode="json") for holiday in holidays),
                key=lambda row: (row["start_date"], row["end_date"] or ""),
            ),
        },
        sort_keys=True,
    )
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
    events = exports.calendar_events(schedule, student_id, holidays)
    if state.fingerprint != fingerprint:
        state.sequence += 1
        state.changed_at = datetime.now(UTC)
        state.fingerprint = fingerprint
        state.events = exports.merge_events(state.events, events)
    content = await asyncio.to_thread(
        exports.render_calendar, state.events, state.sequence, state.changed_at
    )
    await db.commit()
    return content
