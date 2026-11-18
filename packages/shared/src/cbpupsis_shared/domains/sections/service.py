"""Section rules: faculty own-section reads and admin catalog section management."""

from __future__ import annotations

import csv
import io
import logging
import uuid
from collections.abc import Collection
from datetime import UTC, datetime, time

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.exceptions import ConflictError
from cbpupsis_core.pagination import Page
from cbpupsis_shared.domains.academics import client as academics_client
from cbpupsis_shared.domains.audit import client as audit_client
from cbpupsis_shared.domains.auth.exceptions import (
    InsufficientRoleError,
    ResourceNotFoundError,
)
from cbpupsis_shared.domains.curriculum import client as curriculum_client
from cbpupsis_shared.domains.enrollment import client as enrollment_client
from cbpupsis_shared.domains.grading import client as grading_client
from cbpupsis_shared.domains.sections import repository
from cbpupsis_shared.domains.sections.constants import ACADEMIC_RECORD_DISCLOSED
from cbpupsis_shared.domains.sections.exceptions import (
    InvalidRosterOffsetError,
    SectionCapacityBelowEnrolledError,
)
from cbpupsis_shared.domains.sections.schemas import (
    AddDropStatus,
    AdminSectionCreate,
    AdminSectionRead,
    AdminSectionUpdate,
    CourseSectionRead,
    CourseSectionRosterRead,
    EnrollmentStatus,
    FacultyCourseAttemptRead,
    FacultyPrerequisiteRecordRead,
    FacultyStudentRead,
    FacultyStudentRecordRead,
    GradeStatus,
    MeetingSlot,
    OwnedSectionRead,
    RecordSection,
    RosterEntryRead,
    ScheduleSection,
    SectionMeetingInput,
    SectionMeetingRead,
)
from cbpupsis_shared.domains.users import client as users_client

logger = logging.getLogger(__name__)


async def record_sections(db: AsyncSession, ids: Collection[uuid.UUID]):
    return [
        RecordSection.model_validate(row)
        for row in await repository.list_record_sections(db, ids)
    ]


async def schedule_sections(
    db: AsyncSession, ids: list[uuid.UUID]
) -> list[ScheduleSection]:
    """Return section DTOs, never session-bound ORM rows."""
    sections, meetings = await repository.schedule_sections(db, ids)
    by_section: dict[uuid.UUID, list[MeetingSlot]] = {}
    for meeting in meetings:
        by_section.setdefault(meeting.section_id, []).append(
            MeetingSlot.model_validate(meeting)
        )
    return [
        ScheduleSection(
            id=row.id,
            academic_term_id=row.academic_term_id,
            course_id=row.course_id,
            faculty_id=row.faculty_id,
            section_name=row.section_name,
            capacity=row.capacity,
            status=row.status,
            meetings=by_section.get(row.id, []),
        )
        for row in sections
    ]


async def available_sections(
    db: AsyncSession,
    *,
    academic_term_id: uuid.UUID,
    course_ids: Collection[uuid.UUID],
) -> list[ScheduleSection]:
    rows, meetings = await repository.list_available_sections(
        db, academic_term_id=academic_term_id, course_ids=course_ids
    )
    by_section: dict[uuid.UUID, list[MeetingSlot]] = {}
    for meeting in meetings:
        by_section.setdefault(meeting.section_id, []).append(
            MeetingSlot.model_validate(meeting)
        )
    return [
        ScheduleSection(
            id=row.id,
            academic_term_id=row.academic_term_id,
            course_id=row.course_id,
            faculty_id=row.faculty_id,
            section_name=row.section_name,
            capacity=row.capacity,
            status=row.status,
            meetings=by_section.get(row.id, []),
        )
        for row in rows
    ]


async def mark_seats_taken(db: AsyncSession, ids: Collection[uuid.UUID]) -> None:
    await repository.mark_seats_taken(db, ids)


async def lock_for_enrollment(
    db: AsyncSession, ids: Collection[uuid.UUID]
) -> list[ScheduleSection]:
    rows, meetings = await repository.lock_sections_for_enrollment(db, ids)
    by_section: dict[uuid.UUID, list[MeetingSlot]] = {}
    for meeting in meetings:
        by_section.setdefault(meeting.section_id, []).append(
            MeetingSlot.model_validate(meeting)
        )
    return [
        ScheduleSection(
            id=row.id,
            academic_term_id=row.academic_term_id,
            course_id=row.course_id,
            faculty_id=row.faculty_id,
            section_name=row.section_name,
            capacity=row.capacity,
            status=row.status,
            meetings=by_section.get(row.id, []),
        )
        for row in rows
    ]


async def list_own_sections(
    db: AsyncSession, *, faculty_id: uuid.UUID, limit: int, offset: int
) -> Page[CourseSectionRead]:
    """Return a page of the caller's own sections."""
    rows, total = await repository.list_course_sections_for_faculty(
        db, faculty_id, limit=limit, offset=offset
    )
    return Page[CourseSectionRead](
        items=[CourseSectionRead.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_section_roster(
    db: AsyncSession,
    *,
    faculty_id: uuid.UUID,
    section_id: uuid.UUID,
    enrollment_status: EnrollmentStatus | None = None,
    grade_status: GradeStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> CourseSectionRosterRead:
    """Return one filtered roster page after enforcing section ownership."""
    section = await _owned_section(db, faculty_id=faculty_id, section_id=section_id)
    entries = await _filtered_roster_entries(
        db,
        section_id=section_id,
        enrollment_status=enrollment_status,
        grade_status=grade_status,
    )
    total = len(entries)
    if total > 0 and offset >= total:
        raise InvalidRosterOffsetError
    return CourseSectionRosterRead(
        id=section.id,
        section_name=section.section_name,
        capacity=section.capacity,
        status=section.status,
        roster=entries[offset : offset + limit],
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_owned_section(
    db: AsyncSession, *, faculty_id: uuid.UUID, section_id: uuid.UUID
) -> OwnedSectionRead:
    """Return section metadata if assigned to ``faculty_id``, else opaque 404."""
    section = await _owned_section(db, faculty_id=faculty_id, section_id=section_id)
    return OwnedSectionRead.model_validate(section)


async def ids_for_courses(db: AsyncSession, course_ids):
    """Resolve a catalog-owner scoped course set without cross-owner SQL."""
    return await repository.ids_for_courses(db, course_ids)


async def _owned_section(
    db: AsyncSession, *, faculty_id: uuid.UUID, section_id: uuid.UUID
):
    """Use one opaque 404 for a missing section and a section owned by another."""
    section = await repository.get_course_section(db, section_id)
    if section is None or section.faculty_id != faculty_id:
        if section is not None:
            # UUIDs identify the attempt, not private profile data.
            logger.warning(
                "sections.roster_access_refused",
                extra={"faculty_id": str(faculty_id), "section_id": str(section_id)},
            )
        raise ResourceNotFoundError
    return section


def _enrollment_status(value: str) -> EnrollmentStatus:
    normalized = value.strip().lower()
    if normalized in {"enrolled", "enrolled_pending", "officially_enrolled"}:
        return EnrollmentStatus.ENROLLED
    if normalized in {"dropped", "drop"}:
        return EnrollmentStatus.DROPPED
    if normalized in {"withdrawn", "withdraw", "w"}:
        return EnrollmentStatus.WITHDRAWN
    return EnrollmentStatus(value.strip().upper())


async def _filtered_roster_entries(
    db: AsyncSession,
    *,
    section_id: uuid.UUID,
    enrollment_status: EnrollmentStatus | None,
    grade_status: GradeStatus | None,
) -> list[RosterEntryRead]:
    memberships = await enrollment_client.section_roster_enrollments(db, section_id)
    if enrollment_status is not None:
        memberships = [
            row
            for row in memberships
            if _enrollment_status(row.enrollment_status) == enrollment_status
        ]
    grades = await grading_client.section_roster_grades(
        db, {row.enrollment_id for row in memberships}
    )
    if grade_status is not None:
        memberships = [
            row
            for row in memberships
            if grades[row.enrollment_id].grade_status == grade_status.value
        ]
    identities = await users_client.faculty_roster_identities(
        db, {row.student_id for row in memberships}
    )
    add_drop = {
        row.student_id: AddDropStatus(row.status)
        for row in await enrollment_client.section_roster_add_drop(
            db,
            section_id=section_id,
            student_ids=set(identities),
        )
    }
    entries = []
    for membership in memberships:
        identity = identities.get(membership.student_id)
        if identity is None:
            continue
        entries.append(
            RosterEntryRead(
                user_id=membership.student_id,
                student_number=identity.student_number,
                name=identity.name,
                email=identity.email,
                enrollment_status=_enrollment_status(membership.enrollment_status),
                grade_status=GradeStatus(grades[membership.enrollment_id].grade_status),
                add_drop_status=add_drop.get(membership.student_id, AddDropStatus.NONE),
            )
        )
    return sorted(
        entries,
        key=lambda row: (row.name.casefold(), row.student_number, str(row.user_id)),
    )


async def export_section_roster_csv(
    db: AsyncSession,
    *,
    faculty_id: uuid.UUID,
    section_id: uuid.UUID,
    enrollment_status: EnrollmentStatus | None = None,
    grade_status: GradeStatus | None = None,
) -> str:
    """Export every row matching the same filters as the JSON roster."""
    await _owned_section(db, faculty_id=faculty_id, section_id=section_id)
    entries = await _filtered_roster_entries(
        db,
        section_id=section_id,
        enrollment_status=enrollment_status,
        grade_status=grade_status,
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(
        (
            "studentNumber",
            "name",
            "email",
            "enrollmentStatus",
            "gradeStatus",
            "addDropStatus",
        )
    )
    writer.writerows(
        (
            entry.student_number,
            entry.name,
            entry.email,
            entry.enrollment_status.value,
            entry.grade_status.value,
            entry.add_drop_status.value,
        )
        for entry in entries
    )
    return output.getvalue()


async def get_section_student(
    db: AsyncSession,
    *,
    faculty_id: uuid.UUID,
    section_id: uuid.UUID,
    student_id: uuid.UUID,
) -> FacultyStudentRead:
    """Return only the teaching-safe identity fields listed in the contract."""
    await _owned_section(db, faculty_id=faculty_id, section_id=section_id)
    memberships = await enrollment_client.section_roster_enrollments(db, section_id)
    if not any(row.student_id == student_id for row in memberships):
        raise ResourceNotFoundError
    identities = await users_client.faculty_roster_identities(db, {student_id})
    identity = identities.get(student_id)
    if identity is None:
        raise ResourceNotFoundError
    programs = await curriculum_client.programs_by_ids(db, {identity.program_id})
    program = programs.get(identity.program_id)
    if program is None:
        raise ResourceNotFoundError
    return FacultyStudentRead(
        name=identity.name,
        student_number=identity.student_number,
        email=identity.email,
        program=program.name,
        year_level=identity.year_level,
    )


def _faculty_attempt(course, term) -> FacultyCourseAttemptRead:
    outcome = {
        "Passed": "PASSED",
        "Failed": "FAILED",
        "Incomplete": "INCOMPLETE",
        "Withdrawn": "WITHDRAWN",
    }[course.status]
    return FacultyCourseAttemptRead(
        enrollment_id=course.enrollment_id,
        academic_term_id=term.id,
        academic_year=term.academic_year,
        semester=term.semester,
        final_grade=course.final_grade,
        result=outcome,
    )


async def get_section_student_record(
    db: AsyncSession,
    *,
    faculty_id: uuid.UUID,
    section_id: uuid.UUID,
    student_id: uuid.UUID,
) -> FacultyStudentRecordRead:
    """Return course-scoped results and persist one disclosure audit row."""
    section = await _owned_section(db, faculty_id=faculty_id, section_id=section_id)
    current_term = await academics_client.get_current_term(db)
    if current_term is None or section.academic_term_id != current_term.id:
        raise ResourceNotFoundError
    memberships = await enrollment_client.section_roster_enrollments(db, section_id)
    if not any(row.student_id == student_id for row in memberships):
        raise ResourceNotFoundError

    catalog = await curriculum_client.academic_catalog(db, None, {section.course_id})
    prerequisite_ids = {
        edge.prerequisite_course_id
        for edge in catalog.prerequisites
        if edge.course_id == section.course_id and not edge.is_corequisite
    }
    courses = {
        row.id: row
        for row in await curriculum_client.record_courses(db, prerequisite_ids)
    }
    transcript = await grading_client.read_transcript(
        db, student_id=student_id, complete=True
    )
    attempts_by_course: dict[uuid.UUID, list[FacultyCourseAttemptRead]] = {}
    for term_block in transcript.terms:
        for course in term_block.courses:
            if course.course_id in prerequisite_ids | {section.course_id}:
                attempts_by_course.setdefault(course.course_id, []).append(
                    _faculty_attempt(course, term_block.term)
                )

    prerequisites = []
    for prerequisite_id in sorted(prerequisite_ids, key=str):
        course = courses.get(prerequisite_id)
        if course is None:
            continue
        attempts = attempts_by_course.get(prerequisite_id, [])
        result = (
            "PASSED"
            if any(attempt.result == "PASSED" for attempt in attempts)
            else "NOT_PASSED"
            if attempts
            else "NOT_TAKEN"
        )
        prerequisites.append(
            FacultyPrerequisiteRecordRead(
                course_id=course.id,
                course_code=course.course_code,
                title=course.title,
                result=result,
                attempts=attempts,
            )
        )

    result = FacultyStudentRecordRead(
        course_id=section.course_id,
        course_attempts=attempts_by_course.get(section.course_id, []),
        prerequisites=prerequisites,
    )
    occurred_at = datetime.now(UTC)
    audit_client.stage(
        db,
        action=ACADEMIC_RECORD_DISCLOSED,
        actor_id=faculty_id,
        target_type="student_academic_record",
        target_id=student_id,
        occurred_at=occurred_at,
        payload={
            "studentId": str(student_id),
            "sectionId": str(section_id),
            "disclosedAt": occurred_at.isoformat(),
        },
    )
    await db.commit()
    return result


def _normalize_meeting_tuples(
    meetings: list[SectionMeetingInput], default_room: str | None
) -> list[tuple[int, time, time, str | None, str]]:
    result = [
        (
            m.day_of_week,
            m.start_time,
            m.end_time,
            m.room if m.room is not None else default_room,
            m.schedule_type,
        )
        for m in meetings
    ]
    if not result and default_room is not None:
        result.append((0, time(8, 0), time(10, 0), default_room, "lecture"))
    return result


async def _build_admin_section_read(
    db: AsyncSession, section_id: uuid.UUID, enrolled: int = 0
) -> AdminSectionRead:
    section = await repository.get_course_section(db, section_id)
    if section is None:
        raise ResourceNotFoundError
    schedules = await repository.list_section_schedules(db, section_id)
    primary_room = next((s.room for s in schedules if s.room is not None), None)
    return AdminSectionRead(
        id=section.id,
        course_id=section.course_id,
        academic_term_id=section.academic_term_id,
        faculty_id=section.faculty_id,
        section_name=section.section_name,
        capacity=section.capacity,
        enrolled=enrolled,
        seats_left=max(0, section.capacity - enrolled),
        room=primary_room,
        status=section.status,
        meetings=[SectionMeetingRead.model_validate(s) for s in schedules],
    )


async def create_admin_section(
    db: AsyncSession,
    *,
    payload: AdminSectionCreate,
    actor_id: uuid.UUID,
    position: str,
    department_id: str | None,
) -> AdminSectionRead:
    """Create a course section in the caller's department and record one audit row."""
    if position not in {"chairperson", "superadmin"}:
        raise InsufficientRoleError
    course = await curriculum_client.get_course_read(db, payload.course_id)
    if course is None:
        raise ResourceNotFoundError
    if position == "chairperson" and (
        department_id is None or course.department != department_id
    ):
        raise ResourceNotFoundError

    term_id = payload.academic_term_id
    if term_id is None:
        current_term = await academics_client.get_current_term(db)
        term_id = current_term.id if current_term is not None else uuid.uuid4()

    section = repository.add_course_section(
        db,
        academic_term_id=term_id,
        course_id=payload.course_id,
        faculty_id=payload.faculty_id,
        section_name=payload.section_name.strip(),
        capacity=payload.capacity,
        status=payload.status,
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(
            "Section already exists for this course and academic term.",
            code="SECTION_ALREADY_EXISTS",
        ) from exc

    meeting_tuples = _normalize_meeting_tuples(payload.meetings, payload.room)
    if meeting_tuples:
        await repository.replace_section_schedules(
            db, section_id=section.id, meetings=meeting_tuples
        )
        await db.flush()

    occurred_at = datetime.now(UTC)
    new_state = {
        "id": str(section.id),
        "courseId": str(section.course_id),
        "academicTermId": str(section.academic_term_id),
        "facultyId": str(section.faculty_id) if section.faculty_id else None,
        "section_name": section.section_name,
        "sectionName": section.section_name,
        "capacity": section.capacity,
        "room": payload.room,
        "meetingsCount": len(meeting_tuples),
    }
    audit_client.stage(
        db,
        action="catalog.section_created",
        actor_id=actor_id,
        target_type="course_section",
        target_id=section.id,
        occurred_at=occurred_at,
        prior_state=None,
        new_state=new_state,
    )
    await db.commit()
    return await _build_admin_section_read(db, section.id, enrolled=0)


async def update_admin_section(
    db: AsyncSession,
    *,
    section_id: uuid.UUID,
    payload: AdminSectionUpdate,
    actor_id: uuid.UUID,
    position: str,
    department_id: str | None,
) -> AdminSectionRead:
    """Update an in-scope course section, enforcing capacity >= enrolled count."""
    if position not in {"chairperson", "superadmin"}:
        raise InsufficientRoleError
    section = await repository.get_course_section(db, section_id)
    if section is None:
        raise ResourceNotFoundError
    course = await curriculum_client.get_course_read(db, section.course_id)
    if position == "chairperson" and (
        course is None or department_id is None or course.department != department_id
    ):
        raise ResourceNotFoundError

    enrolled_count = await enrollment_client.count_section_enrollments(db, section_id)
    if payload.capacity is not None and payload.capacity < enrolled_count:
        raise SectionCapacityBelowEnrolledError(enrolled=enrolled_count)

    existing_schedules = await repository.list_section_schedules(db, section_id)
    prior_room = next((s.room for s in existing_schedules if s.room is not None), None)
    prior_state = {
        "id": str(section.id),
        "courseId": str(section.course_id),
        "facultyId": str(section.faculty_id) if section.faculty_id else None,
        "section_name": section.section_name,
        "sectionName": section.section_name,
        "capacity": section.capacity,
        "room": prior_room,
        "status": section.status,
        "meetingsCount": len(existing_schedules),
    }

    updates: dict[str, object] = {}
    if payload.section_name is not None:
        updates["section_name"] = payload.section_name.strip()
    if "faculty_id" in payload.model_fields_set:
        updates["faculty_id"] = payload.faculty_id
    if payload.capacity is not None:
        updates["capacity"] = payload.capacity
    if payload.status is not None:
        updates["status"] = payload.status
    if updates:
        repository.update_course_section(section, **updates)

    if payload.meetings is not None:
        default_room = (
            payload.room if "room" in payload.model_fields_set else prior_room
        )
        meeting_tuples = _normalize_meeting_tuples(payload.meetings, default_room)
        await repository.replace_section_schedules(
            db, section_id=section.id, meetings=meeting_tuples
        )
    elif "room" in payload.model_fields_set:
        if existing_schedules:
            await repository.update_section_schedules_room(
                db, section_id=section.id, room=payload.room
            )
        elif payload.room is not None:
            await repository.replace_section_schedules(
                db,
                section_id=section.id,
                meetings=[(0, time(8, 0), time(10, 0), payload.room, "lecture")],
            )

    await db.flush()
    updated_schedules = await repository.list_section_schedules(db, section_id)
    new_room = next((s.room for s in updated_schedules if s.room is not None), None)
    occurred_at = datetime.now(UTC)
    new_state = {
        "id": str(section.id),
        "courseId": str(section.course_id),
        "facultyId": str(section.faculty_id) if section.faculty_id else None,
        "section_name": section.section_name,
        "sectionName": section.section_name,
        "capacity": section.capacity,
        "room": new_room,
        "status": section.status,
        "meetingsCount": len(updated_schedules),
    }
    audit_client.stage(
        db,
        action="catalog.section_updated",
        actor_id=actor_id,
        target_type="course_section",
        target_id=section.id,
        occurred_at=occurred_at,
        prior_state=prior_state,
        new_state=new_state,
    )
    await db.commit()
    return await _build_admin_section_read(db, section.id, enrolled=enrolled_count)
