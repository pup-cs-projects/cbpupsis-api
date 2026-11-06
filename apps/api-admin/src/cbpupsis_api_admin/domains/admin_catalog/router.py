"""Admin catalog HTTP routes for courses, curriculum versions, and sections."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.admin_auth.dependencies import (
    AdminSession,
    require_admin_permission,
)
from cbpupsis_api_admin.domains.admin_catalog import service
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
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.curriculum.constants import (
    APPROVE_SUBSTITUTION,
    MANAGE_CATALOG,
    READ_CATALOG,
)

router = APIRouter()


@router.get(
    "/courses",
    response_model=Page[CatalogCourseRead],
    operation_id="list_admin_catalog_courses",
)
async def list_catalog_courses(
    search: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    admin: AdminSession = Depends(require_admin_permission(READ_CATALOG)),
    db: AsyncSession = Depends(get_db),
) -> Page[CatalogCourseRead]:
    return await service.list_courses(
        db,
        position=admin.position,
        department_id=admin.department_id,
        college_id=admin.college_id,
        search=search,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/courses",
    response_model=CatalogCourseRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_admin_catalog_course",
)
async def create_catalog_course(
    body: CatalogCourseCreate,
    admin: AdminSession = Depends(require_admin_permission(MANAGE_CATALOG)),
    db: AsyncSession = Depends(get_db),
) -> CatalogCourseRead:
    return await service.create_course(
        db,
        payload=body,
        actor_id=admin.id,
        position=admin.position,
        department_id=admin.department_id,
        college_id=admin.college_id,
    )


@router.patch(
    "/courses/{course_id}",
    response_model=CatalogCourseRead,
    operation_id="update_admin_catalog_course",
)
async def update_catalog_course(
    course_id: uuid.UUID,
    body: CatalogCourseUpdate,
    admin: AdminSession = Depends(require_admin_permission(MANAGE_CATALOG)),
    db: AsyncSession = Depends(get_db),
) -> CatalogCourseRead:
    return await service.update_course(
        db,
        course_id=course_id,
        payload=body,
        actor_id=admin.id,
        position=admin.position,
        department_id=admin.department_id,
        college_id=admin.college_id,
    )


@router.post(
    "/curricula/{program_code}/versions",
    response_model=CurriculumVersionRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="publish_admin_curriculum_version",
)
async def publish_curriculum_version(
    program_code: str,
    body: CurriculumVersionCreate,
    admin: AdminSession = Depends(require_admin_permission(MANAGE_CATALOG)),
    db: AsyncSession = Depends(get_db),
) -> CurriculumVersionRead:
    return await service.publish_curriculum_version(
        db,
        program_code=program_code,
        payload=body,
        actor_id=admin.id,
        position=admin.position,
        department_id=admin.department_id,
    )


@router.delete(
    "/curricula/{program_code}/versions/{version_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_admin_curriculum_version",
)
async def delete_curriculum_version(
    program_code: str,
    version_id: uuid.UUID,
    admin: AdminSession = Depends(require_admin_permission(MANAGE_CATALOG)),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await service.delete_curriculum_version(
        db,
        program_code=program_code,
        version_id=version_id,
        actor_id=admin.id,
        position=admin.position,
        department_id=admin.department_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/curricula/versions/{version_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_admin_curriculum_version_by_id",
)
async def delete_curriculum_version_by_id(
    version_id: uuid.UUID,
    admin: AdminSession = Depends(require_admin_permission(MANAGE_CATALOG)),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await service.delete_curriculum_version(
        db,
        program_code=None,
        version_id=version_id,
        actor_id=admin.id,
        position=admin.position,
        department_id=admin.department_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/sections",
    response_model=AdminSectionRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_admin_catalog_section",
)
async def create_catalog_section(
    body: AdminSectionCreate,
    admin: AdminSession = Depends(require_admin_permission(MANAGE_CATALOG)),
    db: AsyncSession = Depends(get_db),
) -> AdminSectionRead:
    return await service.create_section(
        db,
        payload=body,
        actor_id=admin.id,
        position=admin.position,
        department_id=admin.department_id,
    )


@router.patch(
    "/sections/{section_id}",
    response_model=AdminSectionRead,
    operation_id="update_admin_catalog_section",
)
async def update_catalog_section(
    section_id: uuid.UUID,
    body: AdminSectionUpdate,
    admin: AdminSession = Depends(require_admin_permission(MANAGE_CATALOG)),
    db: AsyncSession = Depends(get_db),
) -> AdminSectionRead:
    return await service.update_section(
        db,
        section_id=section_id,
        payload=body,
        actor_id=admin.id,
        position=admin.position,
        department_id=admin.department_id,
    )


@router.post(
    "/substitutions",
    response_model=SubstitutionApprovalRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="approve_admin_course_substitution",
)
async def approve_course_substitution(
    body: SubstitutionApproveRequest,
    admin: AdminSession = Depends(require_admin_permission(APPROVE_SUBSTITUTION)),
    db: AsyncSession = Depends(get_db),
) -> SubstitutionApprovalRead:
    return await service.approve_substitution(
        db,
        payload=body,
        actor_id=admin.id,
        position=admin.position,
        department_id=admin.department_id,
    )


@router.get(
    "/students/{student_id}/degree-progress",
    response_model=StudentDegreeProgressRead,
    operation_id="get_admin_student_degree_progress",
)
async def get_student_degree_progress(
    student_id: uuid.UUID,
    admin: AdminSession = Depends(require_admin_permission(READ_CATALOG)),
    db: AsyncSession = Depends(get_db),
) -> StudentDegreeProgressRead:
    return await service.get_student_degree_progress(
        db,
        student_id=student_id,
        position=admin.position,
        department_id=admin.department_id,
        college_id=admin.college_id,
    )
