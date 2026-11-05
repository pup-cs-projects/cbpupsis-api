"""Business logic for the curriculum domain: programs and their curricula.

Only the program lookup account creation needs exists so far. Program and
curriculum administration is its own epic.
"""

from __future__ import annotations

import uuid
from collections import defaultdict, deque
from collections.abc import Collection, Iterable
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.exceptions import NotFoundError, ValidationError
from cbpupsis_shared.domains.curriculum import repository
from cbpupsis_shared.domains.curriculum.schemas import (
    AcademicCatalog,
    AcademicCourse,
    CurriculumRead,
    PrerequisiteEdge,
    ProgramRead,
    RecordCourse,
    ScheduleCourse,
)


async def record_courses(db: AsyncSession, ids: Collection[uuid.UUID]):
    return [
        RecordCourse.model_validate(row)
        for row in await repository.list_record_courses(db, ids)
    ]


async def schedule_courses(
    db: AsyncSession, ids: list[uuid.UUID]
) -> list[ScheduleCourse]:
    return [
        ScheduleCourse.model_validate(row)
        for row in await repository.schedule_courses(db, ids)
    ]


async def active_programs_by_code(
    db: AsyncSession, codes: Iterable[str]
) -> dict[str, ProgramRead]:
    """Map each upper-cased code that names an active program to that program.

    Codes compare case-insensitively, so ``bscs`` and ``BSCS`` are one program.
    A code missing from the result is unknown or inactive.
    """
    wanted = {code.strip().upper() for code in codes if code and code.strip()}
    rows = await repository.list_active_programs_by_code(db, wanted)
    return {row.code.upper(): ProgramRead.model_validate(row) for row in rows}


async def get_program(db: AsyncSession, program_id: uuid.UUID) -> ProgramRead | None:
    """Return a program by id, or ``None``."""
    row = await repository.get_program(db, program_id)
    return None if row is None else ProgramRead.model_validate(row)


async def get_curriculum(
    db: AsyncSession, curriculum_id: uuid.UUID
) -> CurriculumRead | None:
    """Return a curriculum by id, or ``None``."""
    row = await repository.get_curriculum(db, curriculum_id)
    return None if row is None else CurriculumRead.model_validate(row)


async def get_prescribed_load(
    db: AsyncSession,
    *,
    curriculum_id: uuid.UUID | None,
    year_level: int,
    semester: str,
) -> Decimal:
    """Return prescribed credit units for year level and semester."""
    return await repository.get_prescribed_load(
        db, curriculum_id=curriculum_id, year_level=year_level, semester=semester
    )


def _validate_prerequisite_graph(edges: set[tuple[uuid.UUID, uuid.UUID]]) -> None:
    dependents: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    incoming: dict[uuid.UUID, int] = {}
    for course_id, prerequisite_id in edges:
        dependents[prerequisite_id].add(course_id)
        incoming.setdefault(prerequisite_id, 0)
        incoming[course_id] = incoming.get(course_id, 0) + 1
    ready = deque(node for node, count in incoming.items() if count == 0)
    visited = 0
    while ready:
        node = ready.popleft()
        visited += 1
        for dependent in dependents[node]:
            incoming[dependent] -= 1
            if incoming[dependent] == 0:
                ready.append(dependent)
    if visited != len(incoming):
        raise ValidationError(
            "Prerequisites cannot contain a self-dependency or cycle.",
            code="CURRICULUM_PREREQUISITE_CYCLE",
        )


async def stage_replace_prerequisites(
    db: AsyncSession,
    *,
    course_id: uuid.UUID,
    prerequisite_course_ids: Collection[uuid.UUID],
) -> None:
    """Validate and stage a course's full prerequisite replacement atomically.

    The caller owns commit/rollback. Every catalogue prerequisite writer must
    use this operation, with a READ COMMITTED PostgreSQL transaction. No routes
    or catalogue administration authority are introduced here. Retained edges
    preserve metadata; newly added edges use standard prerequisite defaults.
    """
    wanted = set(prerequisite_course_ids)
    await repository.lock_prerequisite_graph(db)
    courses = await repository.list_record_courses(db, wanted | {course_id})
    if {row.id for row in courses} != wanted | {course_id}:
        raise NotFoundError("Course not found.", code="RESOURCE_NOT_FOUND")
    graph = await repository.prerequisite_graph(db)
    previous = {prerequisite for course, prerequisite in graph if course == course_id}
    proposed = {(course, prereq) for course, prereq in graph if course != course_id}
    proposed.update((course_id, prerequisite) for prerequisite in wanted)
    _validate_prerequisite_graph(proposed)
    await repository.stage_replace_prerequisites(
        db,
        course_id=course_id,
        prerequisite_course_ids=wanted,
        previous_ids=previous,
    )


async def academic_catalog(
    db: AsyncSession,
    curriculum_id: uuid.UUID | None,
    course_ids: Collection[uuid.UUID],
) -> AcademicCatalog:
    courses, edges, placements = await repository.academic_catalog(
        db, curriculum_id, course_ids
    )
    return AcademicCatalog(
        courses=[AcademicCourse.model_validate(row) for row in courses],
        prerequisites=[PrerequisiteEdge.model_validate(row) for row in edges],
        placements=placements,
    )


async def program_ids_in_scope(
    db: AsyncSession,
    *,
    department: str | None = None,
    college: str | None = None,
) -> list[uuid.UUID]:
    return await repository.program_ids_in_scope(
        db, department=department, college=college
    )
