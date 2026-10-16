"""HTTP routing for faculty sections, mounted at /api/v1/sections.

Both checks on every endpoint: the coarse ``ReadCourseSection`` permission plus
an MFA-completed faculty session here, and the ownership check in the service.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_faculty.domains.faculty_auth.dependencies import (
    require_faculty_session,
)
from cbpupsis_api_faculty.domains.sections import service
from cbpupsis_api_faculty.domains.sections.constants import READ_COURSE_SECTION
from cbpupsis_api_faculty.domains.sections.schemas import (
    CourseSectionRead,
    CourseSectionRosterRead,
)
from cbpupsis_core.pagination import Page
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.iam.dependencies import require_permission

router = APIRouter(dependencies=[Depends(require_permission(READ_COURSE_SECTION))])


@router.get("", response_model=Page[CourseSectionRead])
async def list_sections(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(require_faculty_session),
    db: AsyncSession = Depends(get_db),
) -> Page[CourseSectionRead]:
    """List the sections assigned to you."""
    return await service.list_own_sections(
        db, faculty_id=user.id, limit=limit, offset=offset
    )


@router.get("/{section_id}/roster", response_model=CourseSectionRosterRead)
async def section_roster(
    section_id: uuid.UUID,
    user: CurrentUser = Depends(require_faculty_session),
    db: AsyncSession = Depends(get_db),
) -> CourseSectionRosterRead:
    """Return one of your sections with its roster; 404 for anyone else's."""
    return await service.get_section_roster(
        db, faculty_id=user.id, section_id=section_id
    )
