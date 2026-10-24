"""Data access for the curriculum domain. Reads only, today.

Transactions belong to the service: nothing here commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.curriculum import (
    Course,
    Curriculum,
    CurriculumCourse,
    Program,
)


async def list_record_courses(db: AsyncSession, ids: Collection[uuid.UUID]):
    """Historical records retain inactive catalog courses.

    SQL::

        SELECT * FROM courses WHERE id IN (:ids);
    """
    if not ids:
        return []
    return list(await db.scalars(select(Course).where(Course.id.in_(ids))))


async def schedule_courses(
    db: AsyncSession, ids: Collection[uuid.UUID]
) -> list[Course]:
    """Read catalog details for the selected sections, including inactive courses.

    SQL::

        SELECT * FROM courses WHERE id IN (:ids);
    """
    if not ids:
        return []
    return list(await db.scalars(select(Course).where(Course.id.in_(ids))))


async def list_active_programs_by_code(
    db: AsyncSession, codes: Collection[str]
) -> list[Program]:
    """Return the active programs whose code matches one of ``codes``.

    ``codes`` must already be upper-cased; the match ignores the stored case.

    SQL::

        SELECT programs.code, programs.name, programs.department,
               programs.total_units_required, programs.is_active, programs.id
        FROM programs
        WHERE upper(programs.code) IN (__[POSTCOMPILE_upper_1])
          AND programs.is_active IS true
    """
    if not codes:
        return []
    result = await db.scalars(
        select(Program).where(
            func.upper(Program.code).in_(list(codes)), Program.is_active.is_(True)
        )
    )
    return list(result)


async def get_program(db: AsyncSession, program_id: uuid.UUID) -> Program | None:
    """Return a degree program by primary key, or ``None``.

    SQL::

        SELECT * FROM programs WHERE id = :program_id
    """
    return await db.get(Program, program_id)


async def get_curriculum(
    db: AsyncSession, curriculum_id: uuid.UUID
) -> Curriculum | None:
    """Return a curriculum version by primary key, or ``None``.

    SQL::

        SELECT * FROM curricula WHERE id = :curriculum_id
    """
    return await db.get(Curriculum, curriculum_id)


async def get_prescribed_load(
    db: AsyncSession,
    *,
    curriculum_id: uuid.UUID | None,
    year_level: int,
    semester: str,
) -> Decimal:
    """Compute prescribed units load for curriculum year level and semester.

    SQL::

        SELECT COALESCE(SUM(courses.lecture_units + courses.lab_units), 26)
        FROM curriculum_courses
        JOIN courses ON courses.id = curriculum_courses.course_id
        WHERE curriculum_courses.curriculum_id = :curriculum_id
          AND curriculum_courses.year_level = :year_level
          AND curriculum_courses.semester = :semester
    """
    if curriculum_id is None:
        return Decimal("26")
    stmt = (
        select(func.sum(Course.lecture_units + Course.lab_units))
        .select_from(CurriculumCourse)
        .join(Course, Course.id == CurriculumCourse.course_id)
        .where(
            CurriculumCourse.curriculum_id == curriculum_id,
            CurriculumCourse.year_level == year_level,
            CurriculumCourse.semester == semester,
        )
    )
    units = await db.scalar(stmt)
    return Decimal(str(units)) if units else Decimal("26")
