"""Internal client for the curriculum domain: the extraction swap point.

Other domains depend on this module, never on ``service.py`` directly.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.curriculum import service
from cbpupsis_shared.domains.curriculum.schemas import (
    CatalogCourseCreate,
    CatalogCourseRead,
    CatalogCourseUpdate,
    CurriculumRead,
    CurriculumVersionCreate,
    CurriculumVersionRead,
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
    "ProgramRead",
    "ScheduleCourse",
    "StudentDegreeProgressRead",
    "SubstitutionApprovalRead",
    "SubstitutionApproveRequest",
    "active_programs_by_code",
    "approve_course_substitution",
    "create_catalog_course",
    "delete_curriculum_version",
    "get_course_read",
    "get_curriculum",
    "get_prescribed_load",
    "get_program",
    "get_student_degree_progress",
    "list_catalog_courses",
    "publish_curriculum_version",
    "record_courses",
    "schedule_courses",
    "update_catalog_course",
]


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
