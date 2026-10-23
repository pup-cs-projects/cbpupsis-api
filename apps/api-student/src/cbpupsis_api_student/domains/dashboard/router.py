"""HTTP endpoints for the student dashboard."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.dashboard import service
from cbpupsis_api_student.domains.dashboard.exceptions import (
    InvalidInputError,
    ResourceNotFoundError,
)
from cbpupsis_api_student.domains.dashboard.schemas import StudentDashboardResponse
from cbpupsis_api_student.domains.student_auth.dependencies import (
    require_student_session,
)
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.users import client as users_client

router = APIRouter()

YEAR_PATTERN = re.compile(r"^\d{4}-\d{4}$")


def _validate_limits(
    announcement_limit: int,
    event_limit: int,
    activity_limit: int,
    year: str | None,
) -> None:
    if not (1 <= announcement_limit <= 50):
        raise InvalidInputError("announcementLimit must be between 1 and 50.")
    if not (1 <= event_limit <= 50):
        raise InvalidInputError("eventLimit must be between 1 and 50.")
    if not (1 <= activity_limit <= 50):
        raise InvalidInputError("activityLimit must be between 1 and 50.")
    if year is not None and not YEAR_PATTERN.match(year):
        raise InvalidInputError("year parameter is malformed; expected YYYY-YYYY.")


@router.get("/dashboard", response_model=StudentDashboardResponse)
async def get_dashboard(
    semester: str | None = Query(None, description="Target semester name"),
    year: str | None = Query(None, description="Target academic year (YYYY-YYYY)"),
    announcement_limit: int = Query(10, alias="announcementLimit"),
    event_limit: int = Query(10, alias="eventLimit"),
    activity_limit: int = Query(10, alias="activityLimit"),
    user: CurrentUser = Depends(require_student_session),
    db: AsyncSession = Depends(get_db),
) -> StudentDashboardResponse:
    """Return single aggregate dashboard for the authenticated student."""
    _validate_limits(announcement_limit, event_limit, activity_limit, year)
    return await service.get_dashboard(
        db,
        user=user,
        semester=semester,
        year=year,
        announcement_limit=announcement_limit,
        event_limit=event_limit,
        activity_limit=activity_limit,
    )


@router.get("/me/dashboard", response_model=StudentDashboardResponse)
async def get_my_dashboard(
    semester: str | None = Query(None),
    year: str | None = Query(None),
    announcement_limit: int = Query(10, alias="announcementLimit"),
    event_limit: int = Query(10, alias="eventLimit"),
    activity_limit: int = Query(10, alias="activityLimit"),
    user: CurrentUser = Depends(require_student_session),
    db: AsyncSession = Depends(get_db),
) -> StudentDashboardResponse:
    """Alias for /dashboard serving caller's landing view."""
    _validate_limits(announcement_limit, event_limit, activity_limit, year)
    return await service.get_dashboard(
        db,
        user=user,
        semester=semester,
        year=year,
        announcement_limit=announcement_limit,
        event_limit=event_limit,
        activity_limit=activity_limit,
    )


@router.get("/{student_id}/dashboard", response_model=StudentDashboardResponse)
async def get_student_dashboard_by_id(
    student_id: str,
    semester: str | None = Query(None),
    year: str | None = Query(None),
    announcement_limit: int = Query(10, alias="announcementLimit"),
    event_limit: int = Query(10, alias="eventLimit"),
    activity_limit: int = Query(10, alias="activityLimit"),
    user: CurrentUser = Depends(require_student_session),
    db: AsyncSession = Depends(get_db),
) -> StudentDashboardResponse:
    """Return dashboard by student ID with strict ownership enforcement.

    Returns 404 on mismatch to prevent probing.
    """
    _validate_limits(announcement_limit, event_limit, activity_limit, year)

    # Ownership check: callers cannot probe other student IDs
    if student_id.lower() != "me":
        is_owner = str(user.id) == student_id
        if not is_owner:
            profile = await users_client.get_student_profile(db, user.id)
            if profile and profile.student_number == student_id:
                is_owner = True

        if not is_owner:
            # Must answer 404, never 403, per BR-AUTH-005 to prevent probing
            raise ResourceNotFoundError

    return await service.get_dashboard(
        db,
        user=user,
        semester=semester,
        year=year,
        announcement_limit=announcement_limit,
        event_limit=event_limit,
        activity_limit=activity_limit,
    )
