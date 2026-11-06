"""Data access for the curriculum and catalog domain.

Transactions belong to the service: nothing here commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.curriculum import (
    Course,
    CoursePrerequisite,
    CourseSubstitution,
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


async def get_program_by_code(db: AsyncSession, code: str) -> Program | None:
    """Return a degree program by code (case-insensitive), or ``None``.

    SQL::

        SELECT * FROM programs WHERE upper(code) = :code
    """
    return await db.scalar(
        select(Program).where(func.upper(Program.code) == code.strip().upper())
    )


async def list_all_programs(db: AsyncSession) -> list[Program]:
    """List all degree programs for department-to-college resolution.

    SQL::

        SELECT * FROM programs ORDER BY code ASC
    """
    return list(await db.scalars(select(Program).order_by(Program.code.asc())))


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


async def list_all_courses(db: AsyncSession) -> list[Course]:
    """Return all catalog courses ordered by course code.

    SQL::

        SELECT * FROM courses ORDER BY course_code ASC, id ASC
    """
    return list(
        await db.scalars(
            select(Course).order_by(Course.course_code.asc(), Course.id.asc())
        )
    )


async def get_course(db: AsyncSession, course_id: uuid.UUID) -> Course | None:
    """Return one catalog course by id, or ``None``.

    SQL::

        SELECT * FROM courses WHERE id = :course_id
    """
    return await db.get(Course, course_id)


async def get_course_by_code(db: AsyncSession, course_code: str) -> Course | None:
    """Return one catalog course by code (case-insensitive), or ``None``.

    SQL::

        SELECT * FROM courses WHERE upper(course_code) = :course_code
    """
    return await db.scalar(
        select(Course).where(
            func.upper(Course.course_code) == course_code.strip().upper()
        )
    )


def add_course(
    db: AsyncSession,
    *,
    course_code: str,
    title: str,
    description: str | None,
    department: str | None,
    college: str | None,
    lecture_units: Decimal,
    lab_units: Decimal,
    lecture_hours: int = 0,
    lab_hours: int = 0,
    term_offered: str | None = None,
    grading_mode: str,
    is_active: bool,
) -> Course:
    """Stage a new catalog course row.

    SQL::

        INSERT INTO courses (id, course_code, title, description, department,
                             college, lecture_units, lab_units, lecture_hours,
                             lab_hours, term_offered, grading_mode, is_active)
        VALUES (:id, :course_code, :title, :description, :department, :college,
                :lecture_units, :lab_units, :lecture_hours, :lab_hours,
                :term_offered, :grading_mode, :is_active)
    """
    course = Course(
        course_code=course_code,
        title=title,
        description=description,
        department=department,
        college=college,
        lecture_units=lecture_units,
        lab_units=lab_units,
        lecture_hours=lecture_hours,
        lab_hours=lab_hours,
        term_offered=term_offered,
        grading_mode=grading_mode,
        is_active=is_active,
    )
    db.add(course)
    return course


def update_course(course: Course, **fields: object) -> Course:
    """Update fields on a loaded catalog course row.

    SQL::

        UPDATE courses SET title = :title, ... WHERE id = :course_id
    """
    for key, value in fields.items():
        setattr(course, key, value)
    return course


async def list_all_course_prerequisites(
    db: AsyncSession,
) -> list[CoursePrerequisite]:
    """Return all prerequisite and co-requisite edges across the catalog.

    SQL::

        SELECT * FROM course_prerequisites
    """
    return list(await db.scalars(select(CoursePrerequisite)))


async def replace_course_prerequisites(
    db: AsyncSession,
    *,
    course_id: uuid.UUID,
    prerequisite_ids: list[uuid.UUID],
    corequisite_ids: list[uuid.UUID],
) -> None:
    """Replace prerequisite and co-requisite edges for ``course_id``.

    SQL::

        DELETE FROM course_prerequisites WHERE course_id = :course_id;
        INSERT INTO course_prerequisites (course_id, prerequisite_course_id,
                                          is_corequisite)
        VALUES (:course_id, :prerequisite_course_id, :is_corequisite);
    """
    await db.execute(
        delete(CoursePrerequisite).where(CoursePrerequisite.course_id == course_id)
    )
    seen: set[uuid.UUID] = set()
    for prereq_id in prerequisite_ids:
        if prereq_id not in seen:
            seen.add(prereq_id)
            db.add(
                CoursePrerequisite(
                    course_id=course_id,
                    prerequisite_course_id=prereq_id,
                    is_corequisite=False,
                )
            )
    for coreq_id in corequisite_ids:
        if coreq_id not in seen:
            seen.add(coreq_id)
            db.add(
                CoursePrerequisite(
                    course_id=course_id,
                    prerequisite_course_id=coreq_id,
                    is_corequisite=True,
                )
            )


def add_curriculum(
    db: AsyncSession,
    *,
    program_id: uuid.UUID,
    curriculum_year: str,
    effective_admission_year: int,
    is_active: bool = True,
) -> Curriculum:
    """Stage a new curriculum version row.

    SQL::

        INSERT INTO curricula (id, program_id, curriculum_year,
                               effective_admission_year, is_active)
        VALUES (:id, :program_id, :curriculum_year,
                :effective_admission_year, :is_active)
    """
    curriculum = Curriculum(
        program_id=program_id,
        curriculum_year=curriculum_year,
        effective_admission_year=effective_admission_year,
        is_active=is_active,
    )
    db.add(curriculum)
    return curriculum


def add_curriculum_course(
    db: AsyncSession,
    *,
    curriculum_id: uuid.UUID,
    course_id: uuid.UUID,
    year_level: int,
    semester: str,
    course_type: str = "core",
) -> CurriculumCourse:
    """Stage a course mapping inside a curriculum version.

    SQL::

        INSERT INTO curriculum_courses (id, curriculum_id, course_id,
                                        year_level, semester, course_type)
        VALUES (:id, :curriculum_id, :course_id, :year_level, :semester,
                :course_type)
    """
    item = CurriculumCourse(
        curriculum_id=curriculum_id,
        course_id=course_id,
        year_level=year_level,
        semester=semester,
        course_type=course_type,
    )
    db.add(item)
    return item


async def list_curriculum_courses(
    db: AsyncSession, curriculum_id: uuid.UUID
) -> list[tuple[CurriculumCourse, Course]]:
    """Return mapped curriculum courses and their catalog definitions.

    SQL::

        SELECT curriculum_courses.*, courses.*
        FROM curriculum_courses
        JOIN courses ON courses.id = curriculum_courses.course_id
        WHERE curriculum_courses.curriculum_id = :curriculum_id
        ORDER BY curriculum_courses.year_level ASC, courses.course_code ASC
    """
    result = await db.execute(
        select(CurriculumCourse, Course)
        .join(Course, Course.id == CurriculumCourse.course_id)
        .where(CurriculumCourse.curriculum_id == curriculum_id)
        .order_by(CurriculumCourse.year_level.asc(), Course.course_code.asc())
    )
    return [(row[0], row[1]) for row in result.all()]


async def list_all_curriculum_course_pairs(
    db: AsyncSession,
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Return (program_id, course_id) pairs mapped across curricula.

    SQL::

        SELECT curricula.program_id, curriculum_courses.course_id
        FROM curriculum_courses
        JOIN curricula ON curricula.id = curriculum_courses.curriculum_id
    """
    result = await db.execute(
        select(Curriculum.program_id, CurriculumCourse.course_id).join(
            Curriculum, Curriculum.id == CurriculumCourse.curriculum_id
        )
    )
    return [(row[0], row[1]) for row in result.all()]


async def delete_curriculum(db: AsyncSession, curriculum: Curriculum) -> None:
    """Delete an unused curriculum version row.

    SQL::

        DELETE FROM curricula WHERE id = :curriculum_id
    """
    await db.delete(curriculum)


async def list_substitutions_for_students(
    db: AsyncSession, student_ids: Collection[uuid.UUID]
) -> list[CourseSubstitution]:
    """List approved course substitutions for the given students.

    SQL::

        SELECT * FROM course_substitutions
        WHERE student_id IN (:student_ids)
    """
    if not student_ids:
        return []
    return list(
        await db.scalars(
            select(CourseSubstitution).where(
                CourseSubstitution.student_id.in_(list(student_ids))
            )
        )
    )


async def upsert_course_substitution(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    original_course_id: uuid.UUID,
    substitute_course_id: uuid.UUID,
    approved_by: uuid.UUID,
) -> CourseSubstitution:
    """Create or update a student's course substitution for ``original_course_id``.

    SQL::

        SELECT * FROM course_substitutions
        WHERE student_id = :student_id
          AND original_course_id = :original_course_id;
        INSERT INTO course_substitutions (id, student_id, original_course_id,
                                          substitute_course_id, approved_by)
        VALUES (:id, :student_id, :original_course_id,
                :substitute_course_id, :approved_by);
    """
    existing = await db.scalar(
        select(CourseSubstitution).where(
            CourseSubstitution.student_id == student_id,
            CourseSubstitution.original_course_id == original_course_id,
        )
    )
    if existing is not None:
        existing.substitute_course_id = substitute_course_id
        existing.approved_by = approved_by
        return existing
    row = CourseSubstitution(
        student_id=student_id,
        original_course_id=original_course_id,
        substitute_course_id=substitute_course_id,
        approved_by=approved_by,
    )
    db.add(row)
    return row


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
