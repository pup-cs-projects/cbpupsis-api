"""Data access for the curriculum domain. Reads only, today.

Transactions belong to the service: nothing here commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from decimal import Decimal

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.curriculum import (
    Course,
    CoursePrerequisite,
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


async def lock_prerequisite_graph(db: AsyncSession) -> None:
    """Serialize all guarded catalogue graph writers until their transaction ends.

    READ COMMITTED ensures a writer waiting for the lock then reads its
    predecessor's committed edges, even when its transaction started earlier.
    SQLite is supported for focused tests, not as concurrency evidence.

    SQL::

        SELECT pg_advisory_xact_lock(99002001);
    """
    if db.get_bind().dialect.name == "sqlite":
        return
    if db.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Prerequisite graph writes require PostgreSQL")
    connection = await db.connection()
    if await connection.get_isolation_level() != "READ COMMITTED":
        raise RuntimeError("Prerequisite graph writes require READ COMMITTED")
    await db.execute(text("SELECT pg_advisory_xact_lock(99002001)"))


async def prerequisite_graph(
    db: AsyncSession,
) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """Read the complete catalogue graph after taking the graph writer lock.

    SQL::

        SELECT course_id, prerequisite_course_id FROM course_prerequisites;
    """
    return {
        (row.course_id, row.prerequisite_course_id)
        for row in await db.execute(
            select(
                CoursePrerequisite.course_id,
                CoursePrerequisite.prerequisite_course_id,
            )
        )
    }


async def stage_replace_prerequisites(
    db: AsyncSession,
    *,
    course_id: uuid.UUID,
    prerequisite_course_ids: Collection[uuid.UUID],
    previous_ids: Collection[uuid.UUID],
) -> None:
    """Stage a validated replacement, preserving retained edges' metadata.

    The service holds the graph lock and validates before calling this writer.
    New edges use the catalogue's standard 3.00 prerequisite defaults.

    SQL::

        DELETE FROM course_prerequisites WHERE course_id = :course_id
          AND prerequisite_course_id IN (:removed_ids);
        INSERT INTO course_prerequisites
          (course_id, prerequisite_course_id, min_grade, is_corequisite)
        VALUES (:course_id, :new_id, '3.00', false);
    """
    wanted, previous = set(prerequisite_course_ids), set(previous_ids)
    removed = previous - wanted
    if removed:
        await db.execute(
            delete(CoursePrerequisite).where(
                CoursePrerequisite.course_id == course_id,
                CoursePrerequisite.prerequisite_course_id.in_(removed),
            )
        )
    db.add_all(
        CoursePrerequisite(course_id=course_id, prerequisite_course_id=prerequisite_id)
        for prerequisite_id in wanted - previous
    )
    await db.flush()


async def academic_catalog(
    db: AsyncSession,
    curriculum_id: uuid.UUID | None,
    course_ids: Collection[uuid.UUID],
) -> tuple[list[Course], list[CoursePrerequisite], dict[uuid.UUID, int]]:
    """Batch the student's curriculum, selected edges, and prerequisite courses.

    No classification is inferred from codes, titles or student year level.

    SQL::

        SELECT course_id, year_level FROM curriculum_courses
        WHERE curriculum_id = :curriculum_id;
        SELECT * FROM course_prerequisites WHERE course_id IN (:course_ids);
        SELECT * FROM courses WHERE id IN (:curriculum_selected_and_prerequisite_ids);
    """
    placements = (
        dict(
            (
                await db.execute(
                    select(
                        CurriculumCourse.course_id, CurriculumCourse.year_level
                    ).where(CurriculumCourse.curriculum_id == curriculum_id)
                )
            ).all()
        )
        if curriculum_id is not None
        else {}
    )
    edges = (
        list(
            await db.scalars(
                select(CoursePrerequisite).where(
                    CoursePrerequisite.course_id.in_(course_ids)
                )
            )
        )
        if course_ids
        else []
    )
    wanted = set(placements) | set(course_ids)
    wanted.update(edge.prerequisite_course_id for edge in edges)
    courses = await list_record_courses(db, wanted)
    return courses, edges, placements


async def program_ids_in_scope(
    db: AsyncSession,
    *,
    department: str | None = None,
    college: str | None = None,
) -> list[uuid.UUID]:
    """Read explicit department/college membership, never equate the two.

    Unreviewed or blank program scope metadata grants no access. This operation
    intentionally does not implement institution-wide access: the caller must
    explicitly authorize that reviewer position.

    SQL::

        SELECT id FROM programs WHERE department IS NOT NULL AND department != ''
          AND college IS NOT NULL AND college != ''
          AND department = :department AND college = :college;
    """
    if department is None and college is None:
        return []
    if (department is not None and not department.strip()) or (
        college is not None and not college.strip()
    ):
        return []
    stmt = select(Program.id).where(
        Program.department.is_not(None),
        Program.department != "",
        Program.college.is_not(None),
        Program.college != "",
    )
    if department is not None:
        stmt = stmt.where(Program.department == department)
    if college is not None:
        stmt = stmt.where(Program.college == college)
    return list(await db.scalars(stmt))
