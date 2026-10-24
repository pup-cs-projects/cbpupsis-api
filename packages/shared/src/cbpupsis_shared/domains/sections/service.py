"""Section rules: a faculty member sees only the sections assigned to them.

Someone else's section answers 404 exactly like a missing one, so a caller
cannot learn which section ids exist (US-102, AC-102.7).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Collection

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.pagination import Page
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.sections import repository
from cbpupsis_shared.domains.sections.schemas import (
    CourseSectionRead,
    CourseSectionRosterRead,
    MeetingSlot,
    RecordSection,
    ScheduleSection,
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
