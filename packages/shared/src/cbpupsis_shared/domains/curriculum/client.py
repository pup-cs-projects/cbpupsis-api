"""Internal client for the curriculum domain: the extraction swap point.

Other domains depend on this module, never on ``service.py`` directly.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.curriculum import service
from cbpupsis_shared.domains.curriculum.schemas import (
    AcademicCatalog,
    CatalogCourseCreate,
    CatalogCourseRead,
    CatalogCourseUpdate,
    CurriculumRead,
    CurriculumVersionCreate,
    CurriculumVersionRead,
    EnrollmentCourse,
    ProgramRead,
    ScheduleCourse,
    StudentDegreeProgressRead,
    SubstitutionApprovalRead,
    SubstitutionApproveRequest,
)
from cbpupsis_shared.domains.curriculum.service import (
    approve_course_substitution,
    create_catalog_course,
    delete_curriculum_version,
    get_course_read,
    get_student_degree_progress,
    list_catalog_courses,
    publish_curriculum_version,
    record_courses,
    update_catalog_course,
)

__all__ = [
    "CatalogCourseCreate",
    "CatalogCourseRead",
    "CatalogCourseUpdate",
    "CurriculumRead",
    "CurriculumVersionCreate",
    "CurriculumVersionRead",
    "EnrollmentCourse",
    "ProgramRead",
    "ScheduleCourse",
    "StudentDegreeProgressRead",
    "SubstitutionApprovalRead",
    "SubstitutionApproveRequest",
    "academic_catalog",
    "active_curriculum_courses",
    "active_programs_by_code",
    "approve_course_substitution",
    "create_catalog_course",
    "delete_curriculum_version",
    "enrollment_courses",
    "get_course_read",
    "get_curriculum",
    "get_prescribed_load",
    "get_program",
    "get_student_degree_progress",
    "list_catalog_courses",
    "list_curriculum_course_ids",
    "program_ids_in_scope",
    "programs_by_ids",
    "publish_curriculum_version",
    "record_courses",
    "schedule_courses",
    "stage_replace_prerequisites",
    "update_catalog_course",
]


async def programs_by_ids(
    db: AsyncSession, ids: set[uuid.UUID]
) -> dict[uuid.UUID, ProgramRead]:
    return await service.programs_by_ids(db, ids)


async def enrollment_courses(
    db: AsyncSession,
    *,
    curriculum_id: uuid.UUID | None,
    program_id: uuid.UUID,
    year_level: int,
    semester: str,
) -> list[EnrollmentCourse]:
    return await service.enrollment_courses(
        db,
        curriculum_id=curriculum_id,
        program_id=program_id,
        year_level=year_level,
        semester=semester,
    )


async def active_curriculum_courses(
    db: AsyncSession,
    *,
    curriculum_id: uuid.UUID | None,
    program_id: uuid.UUID,
) -> list[EnrollmentCourse]:
    return await service.active_curriculum_courses(
        db,
        curriculum_id=curriculum_id,
        program_id=program_id,
    )


async def schedule_courses(
    db: AsyncSession, ids: list[uuid.UUID]
) -> list[ScheduleCourse]:
    return await service.schedule_courses(db, ids)


async def active_programs_by_code(
    db: AsyncSession, codes: Iterable[str]
) -> dict[str, ProgramRead]:
    return await service.active_programs_by_code(db, codes)


async def get_program(db: AsyncSession, program_id: uuid.UUID) -> ProgramRead | None:
    return await service.get_program(db, program_id)


async def get_curriculum(
    db: AsyncSession, curriculum_id: uuid.UUID
) -> CurriculumRead | None:
    return await service.get_curriculum(db, curriculum_id)


async def get_prescribed_load(
    db: AsyncSession,
    *,
    curriculum_id: uuid.UUID | None,
    year_level: int,
    semester: str,
) -> Decimal:
    return await service.get_prescribed_load(
        db, curriculum_id=curriculum_id, year_level=year_level, semester=semester
    )


async def stage_replace_prerequisites(
    db: AsyncSession,
    *,
    course_id: uuid.UUID,
    prerequisite_course_ids: Collection[uuid.UUID],
) -> None:
    """Guarded catalogue write; caller owns the transaction's commit/rollback."""
    await service.stage_replace_prerequisites(
        db, course_id=course_id, prerequisite_course_ids=prerequisite_course_ids
    )


async def academic_catalog(
    db: AsyncSession,
    curriculum_id: uuid.UUID | None,
    course_ids: Collection[uuid.UUID],
) -> AcademicCatalog:
    return await service.academic_catalog(db, curriculum_id, course_ids)


async def program_ids_in_scope(
    db: AsyncSession,
    *,
    department: str | None = None,
    college: str | None = None,
) -> list[uuid.UUID]:
    return await service.program_ids_in_scope(
        db, department=department, college=college
    )


async def list_curriculum_course_ids(
    db: AsyncSession, curriculum_id: uuid.UUID
) -> set[uuid.UUID]:
    return await service.list_curriculum_course_ids(db, curriculum_id)
