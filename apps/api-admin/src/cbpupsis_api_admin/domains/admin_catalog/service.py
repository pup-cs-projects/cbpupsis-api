"""Application-layer orchestration for admin catalog routes."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.admin_catalog.schemas import (
    AdminSectionCreate,
    AdminSectionRead,
    AdminSectionUpdate,
    CatalogCourseCreate,
    CatalogCourseRead,
    CatalogCourseUpdate,
    CurriculumVersionCreate,
    CurriculumVersionRead,
    StudentDegreeProgressRead,
    SubstitutionApprovalRead,
    SubstitutionApproveRequest,
)
from cbpupsis_core.pagination import Page
from cbpupsis_shared.domains.curriculum import client as curriculum_client
from cbpupsis_shared.domains.sections import client as sections_client


async def list_courses(
    db: AsyncSession,
    *,
    position: str,
    department_id: str | None,
    college_id: str | None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Page[CatalogCourseRead]:
    items = await curriculum_client.list_catalog_courses(
        db,
        position=position,
        department_id=department_id,
        college_id=college_id,
    )
    if search and search.strip():
        needle = search.strip().lower()
        items = [
            item
            for item in items
            if needle in item.course_code.lower() or needle in item.title.lower()
        ]
    total = len(items)
    return Page[CatalogCourseRead](
        items=items[offset : offset + limit],
        total=total,
        limit=limit,
        offset=offset,
    )


async def create_course(
    db: AsyncSession,
    *,
    payload: CatalogCourseCreate,
    actor_id: uuid.UUID,
    position: str,
    department_id: str | None,
    college_id: str | None,
) -> CatalogCourseRead:
    return await curriculum_client.create_catalog_course(
        db,
        payload=payload,
        actor_id=actor_id,
        position=position,
        department_id=department_id,
        college_id=college_id,
    )


async def update_course(
    db: AsyncSession,
    *,
    course_id: uuid.UUID,
    payload: CatalogCourseUpdate,
    actor_id: uuid.UUID,
    position: str,
    department_id: str | None,
    college_id: str | None,
) -> CatalogCourseRead:
    return await curriculum_client.update_catalog_course(
        db,
        course_id=course_id,
        payload=payload,
        actor_id=actor_id,
        position=position,
        department_id=department_id,
        college_id=college_id,
    )


async def publish_curriculum_version(
    db: AsyncSession,
    *,
    program_code: str,
    payload: CurriculumVersionCreate,
    actor_id: uuid.UUID,
    position: str,
    department_id: str | None,
) -> CurriculumVersionRead:
    return await curriculum_client.publish_curriculum_version(
        db,
        program_code=program_code,
        payload=payload,
        actor_id=actor_id,
        position=position,
        department_id=department_id,
    )


async def delete_curriculum_version(
    db: AsyncSession,
    *,
    program_code: str | None = None,
    version_id: uuid.UUID,
    actor_id: uuid.UUID,
    position: str,
    department_id: str | None,
) -> None:
    await curriculum_client.delete_curriculum_version(
        db,
        program_code=program_code,
        version_id=version_id,
        actor_id=actor_id,
        position=position,
        department_id=department_id,
    )


async def create_section(
    db: AsyncSession,
    *,
    payload: AdminSectionCreate,
    actor_id: uuid.UUID,
    position: str,
    department_id: str | None,
) -> AdminSectionRead:
    return await sections_client.create_admin_section(
        db,
        payload=payload,
        actor_id=actor_id,
        position=position,
        department_id=department_id,
    )


async def update_section(
    db: AsyncSession,
    *,
    section_id: uuid.UUID,
    payload: AdminSectionUpdate,
    actor_id: uuid.UUID,
    position: str,
    department_id: str | None,
) -> AdminSectionRead:
    return await sections_client.update_admin_section(
        db,
        section_id=section_id,
        payload=payload,
        actor_id=actor_id,
        position=position,
        department_id=department_id,
    )


async def approve_substitution(
    db: AsyncSession,
    *,
    payload: SubstitutionApproveRequest,
    actor_id: uuid.UUID,
    position: str,
    department_id: str | None,
) -> SubstitutionApprovalRead:
    return await curriculum_client.approve_course_substitution(
        db,
        payload=payload,
        actor_id=actor_id,
        position=position,
        department_id=department_id,
    )


async def get_student_degree_progress(
    db: AsyncSession, *, student_id: uuid.UUID
) -> StudentDegreeProgressRead:
    return await curriculum_client.get_student_degree_progress(db, student_id)
