"""HTTP routing for student landing dashboard, mounted at /api/v1/student."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.dashboard import service
from cbpupsis_api_student.domains.dashboard.constants import DEFAULT_LIMIT
from cbpupsis_api_student.domains.dashboard.schemas import StudentDashboardResponse
from cbpupsis_api_student.domains.student_auth.dependencies import (
    require_student_session,
)
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser

router = APIRouter()


@router.get(
    "/dashboard",
    response_model=StudentDashboardResponse,
    operation_id="get_student_dashboard",
)
async def get_student_dashboard(
    semester: str | None = Query(None, description="Target semester name"),
    year: str | None = Query(None, description="Target academic year (YYYY-YYYY)"),
    announcement_limit: int = Query(DEFAULT_LIMIT, alias="announcementLimit"),
    event_limit: int = Query(DEFAULT_LIMIT, alias="eventLimit"),
    activity_limit: int = Query(DEFAULT_LIMIT, alias="activityLimit"),
    user: CurrentUser = Depends(require_student_session),
    db: AsyncSession = Depends(get_db),
) -> StudentDashboardResponse:
    """Return single aggregate dashboard for the authenticated student (US-101)."""
    return await service.get_dashboard(
        db,
        user=user,
        semester=semester,
        year=year,
        announcement_limit=announcement_limit,
        event_limit=event_limit,
        activity_limit=activity_limit,
    )


@router.get(
    "/me/dashboard",
    response_model=StudentDashboardResponse,
    operation_id="get_my_student_dashboard",
)
async def get_my_student_dashboard(
    semester: str | None = Query(None, description="Target semester name"),
    year: str | None = Query(None, description="Target academic year (YYYY-YYYY)"),
    announcement_limit: int = Query(DEFAULT_LIMIT, alias="announcementLimit"),
    event_limit: int = Query(DEFAULT_LIMIT, alias="eventLimit"),
    activity_limit: int = Query(DEFAULT_LIMIT, alias="activityLimit"),
    user: CurrentUser = Depends(require_student_session),
    db: AsyncSession = Depends(get_db),
) -> StudentDashboardResponse:
    """Alias for /dashboard serving caller's landing view (US-101)."""
    return await service.get_dashboard(
        db,
        user=user,
        student_id="me",
        semester=semester,
        year=year,
        announcement_limit=announcement_limit,
        event_limit=event_limit,
        activity_limit=activity_limit,
    )


@router.get(
    "/{student_id}/dashboard",
    response_model=StudentDashboardResponse,
    operation_id="get_student_dashboard_by_id",
)
async def get_student_dashboard_by_id(
    student_id: str,
    semester: str | None = Query(None, description="Target semester name"),
    year: str | None = Query(None, description="Target academic year (YYYY-YYYY)"),
    announcement_limit: int = Query(DEFAULT_LIMIT, alias="announcementLimit"),
    event_limit: int = Query(DEFAULT_LIMIT, alias="eventLimit"),
    activity_limit: int = Query(DEFAULT_LIMIT, alias="activityLimit"),
    user: CurrentUser = Depends(require_student_session),
    db: AsyncSession = Depends(get_db),
) -> StudentDashboardResponse:
    """Return dashboard by student ID with strict ownership enforcement.

    Enforces BR-AUTH-005 anti-probing rules.
    """
    return await service.get_dashboard(
        db,
        user=user,
        student_id=student_id,
        semester=semester,
        year=year,
        announcement_limit=announcement_limit,
        event_limit=event_limit,
        activity_limit=activity_limit,
    )
