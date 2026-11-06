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


async def list_programs_by_ids(db: AsyncSession, ids: set[uuid.UUID]):
    """Read program scope metadata for a bounded id set.

    SQL::

        SELECT * FROM programs WHERE id IN (:ids);
    """
    if not ids:
        return []
    return list(await db.scalars(select(Program).where(Program.id.in_(ids))))


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

        SELECT SUM(courses.lecture_units + courses.lab_units)
        FROM curriculum_courses
        JOIN courses ON courses.id = curriculum_courses.course_id
        WHERE curriculum_courses.curriculum_id = :curriculum_id
          AND curriculum_courses.year_level = :year_level
          AND curriculum_courses.semester = :semester
    """
    if curriculum_id is None:
        return Decimal("0")
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
    return Decimal(str(units)) if units is not None else Decimal("0")


async def list_enrollment_courses(
    db: AsyncSession,
    *,
    curriculum_id: uuid.UUID | None,
    program_id: uuid.UUID,
    year_level: int,
    semester: str,
) -> list[Course]:
    """Return courses in the student's curriculum template for one term.

    SQL::

        SELECT courses.*
        FROM courses
        JOIN curriculum_courses
          ON curriculum_courses.course_id = courses.id
        JOIN curricula
          ON curricula.id = curriculum_courses.curriculum_id
        WHERE curricula.id = :curriculum_id
          AND curricula.program_id = :program_id
          AND curriculum_courses.year_level = :year_level
          AND curriculum_courses.semester = :semester
          AND courses.is_active IS true
        ORDER BY courses.course_code, courses.id;
    """
    if curriculum_id is None:
        return []
    result = await db.scalars(
        select(Course)
        .join(CurriculumCourse, CurriculumCourse.course_id == Course.id)
        .join(Curriculum, Curriculum.id == CurriculumCourse.curriculum_id)
        .where(
            Curriculum.id == curriculum_id,
            Curriculum.program_id == program_id,
            CurriculumCourse.year_level == year_level,
            CurriculumCourse.semester == semester,
            Course.is_active.is_(True),
        )
        .order_by(Course.course_code, Course.id)
    )
    return list(result)


async def list_curriculum_course_ids(
    db: AsyncSession, curriculum_id: uuid.UUID
) -> set[uuid.UUID]:
    """Return all course IDs mapped to the specified curriculum.

    SQL::

        SELECT course_id FROM curriculum_courses WHERE curriculum_id = :curriculum_id;
    """
    stmt = select(CurriculumCourse.course_id).where(
        CurriculumCourse.curriculum_id == curriculum_id
    )
    return set(await db.scalars(stmt))
