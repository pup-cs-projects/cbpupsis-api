"""Data access for the sections domain. CRUD only: no rules, no authorization."""

from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.sections import CourseSection


async def list_record_sections(db: AsyncSession, ids: Collection[uuid.UUID]):
    """Read section-to-course/term identifiers in one batch.

    SQL::

        SELECT * FROM course_sections WHERE id IN (:ids);
    """
    if not ids:
        return []
    return list(
        await db.scalars(select(CourseSection).where(CourseSection.id.in_(ids)))
    )


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
