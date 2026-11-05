"""Section rules: faculty own-section reads and admin catalog section management."""

from __future__ import annotations

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
from cbpupsis_shared.domains.sections import repository
from cbpupsis_shared.domains.sections.exceptions import (
    SectionCapacityBelowEnrolledError,
)
from cbpupsis_shared.domains.sections.schemas import (
    AdminSectionCreate,
    AdminSectionRead,
    AdminSectionUpdate,
    CourseSectionRead,
    CourseSectionRosterRead,
    MeetingSlot,
    RecordSection,
    ScheduleSection,
    SectionMeetingInput,
    SectionMeetingRead,
)

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
            meetings=by_section.get(row.id, []),
        )
        for row in sections
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
    db: AsyncSession, *, faculty_id: uuid.UUID, section_id: uuid.UUID
) -> CourseSectionRosterRead:
    """Return a section the caller teaches, or 404 for any other section."""
    section = await repository.get_course_section(db, section_id)
    if section is None or section.faculty_id != faculty_id:
        if section is not None:
            # UUIDs identify the attempt, not private profile data.
            logger.warning(
                "sections.roster_access_refused",
                extra={"faculty_id": str(faculty_id), "section_id": str(section_id)},
            )
        raise ResourceNotFoundError
    return CourseSectionRosterRead.model_validate(section)


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
