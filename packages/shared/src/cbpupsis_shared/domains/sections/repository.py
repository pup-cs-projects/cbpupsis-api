"""Data access for the sections domain. CRUD only: no rules, no authorization."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.sections import CourseSection, SectionSchedule


async def schedule_sections(db: AsyncSession, ids: list[uuid.UUID]):
    """Read section metadata and meeting slots in two bounded queries.

    SQL::

        SELECT * FROM course_sections WHERE id IN (:ids);
        SELECT * FROM section_schedules WHERE section_id IN (:ids)
        ORDER BY day_of_week, start_time, id LIMIT 1001;
    """
    if not ids:
        return [], []
    sections = await db.scalars(select(CourseSection).where(CourseSection.id.in_(ids)))
    meetings = await db.scalars(
        select(SectionSchedule)
        .where(SectionSchedule.section_id.in_(ids))
        .order_by(
            SectionSchedule.day_of_week, SectionSchedule.start_time, SectionSchedule.id
        )
        .limit(1001)
    )
    return list(sections), list(meetings)


async def get_course_section(
    db: AsyncSession, section_id: uuid.UUID
) -> CourseSection | None:
    """Return one section by id, or ``None``.

    SQL::

        SELECT * FROM course_sections WHERE id = :section_id
    """
    return await db.get(CourseSection, section_id)


async def list_course_sections_for_faculty(
    db: AsyncSession, faculty_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[CourseSection], int]:
    """Return one page of a faculty member's sections and the total count.

    SQL::

        SELECT count(*) FROM course_sections WHERE faculty_id = :faculty_id;

        SELECT * FROM course_sections WHERE faculty_id = :faculty_id
        ORDER BY section_name, id
        LIMIT :limit OFFSET :offset
    """
    total = await db.scalar(
        select(func.count())
        .select_from(CourseSection)
        .where(CourseSection.faculty_id == faculty_id)
    )
    result = await db.scalars(
        select(CourseSection)
        .where(CourseSection.faculty_id == faculty_id)
        .order_by(CourseSection.section_name, CourseSection.id)
        .limit(limit)
        .offset(offset)
    )
    return list(result.all()), total or 0
