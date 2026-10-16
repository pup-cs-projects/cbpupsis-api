"""Section rules: a faculty member sees only the sections assigned to them.

Someone else's section answers 404 exactly like a missing one, so a caller
cannot learn which section ids exist (US-102, AC-102.7).
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_faculty.domains.sections import repository
from cbpupsis_api_faculty.domains.sections.schemas import (
    CourseSectionRead,
    CourseSectionRosterRead,
)
from cbpupsis_core.pagination import Page
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError

logger = logging.getLogger(__name__)


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
            # Ids only: the event is the attempt, not who the records belong to.
            logger.warning(
                "sections.roster_access_refused",
                extra={"faculty_id": str(faculty_id), "section_id": str(section_id)},
            )
        raise ResourceNotFoundError
    return CourseSectionRosterRead.model_validate(section)
