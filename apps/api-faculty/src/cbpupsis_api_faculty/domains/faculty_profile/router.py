"""HTTP routing for faculty profile self-service (US-FAC-005)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_faculty.domains.faculty_auth.dependencies import (
    require_faculty_session,
)
from cbpupsis_api_faculty.domains.faculty_profile import service
from cbpupsis_api_faculty.domains.faculty_profile.schemas import FacultyProfileRead
from cbpupsis_core.middleware import client_ip_or_none
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.iam.dependencies import require_permission
from cbpupsis_shared.domains.users.constants import READ_PROFILE, UPDATE_PROFILE

router = APIRouter(
    dependencies=[Depends(require_faculty_session)],
    responses={
        401: {
            "description": "Session expired (AUTH_SESSION_EXPIRED) or MFA required "
            "(AUTH_MFA_REQUIRED)."
        },
        403: {
            "description": "Wrong role (AUTH_INSUFFICIENT_ROLE), missing permission, "
            "or locked field (PROFILE_FIELD_LOCKED)."
        },
    },
)


@router.get(
    "",
    response_model=FacultyProfileRead,
    operation_id="get_faculty_profile",
    summary="Get Faculty Profile",
    dependencies=[Depends(require_permission(READ_PROFILE))],
)
async def get_faculty_profile(
    user: CurrentUser = Depends(require_faculty_session),
    db: AsyncSession = Depends(get_db),
) -> FacultyProfileRead:
    """Return the signed-in faculty member's profile and field editability flags."""
    return await service.get_profile(db, actor_id=user.id)


@router.patch(
    "",
    response_model=FacultyProfileRead,
    operation_id="update_faculty_profile",
    summary="Update Faculty Profile",
    dependencies=[Depends(require_permission(UPDATE_PROFILE))],
)
async def update_faculty_profile(
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    user: CurrentUser = Depends(require_faculty_session),
    db: AsyncSession = Depends(get_db),
) -> FacultyProfileRead:
    """Update editable faculty profile fields (title, location, hours, quals)."""
    return await service.update_profile(
        db,
        actor_id=user.id,
        payload=payload,
        ip_address=client_ip_or_none(request),
    )
