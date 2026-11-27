"""HTTP routing for student landing dashboard, mounted at /api/v1/student."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.dashboard import service
from cbpupsis_api_student.domains.dashboard.constants import (
    DEFAULT_LIMIT,
    READ_DASHBOARD,
)
from cbpupsis_api_student.domains.dashboard.schemas import StudentDashboardResponse
from cbpupsis_api_student.domains.student_auth.dependencies import (
    require_student_session,
)
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.iam.dependencies import require_permission


async def private_response(request: Request) -> None:
    """Include authorization and validation errors in the no-store policy."""
    request.state.private_response = True


router = APIRouter(
    dependencies=[
        Depends(private_response),
        Depends(require_student_session),
        Depends(require_permission(READ_DASHBOARD)),
    ],
    responses={
        401: {"description": "Missing, invalid, expired, or inactive student session."},
        403: {
            "description": "Caller is not a student or lacks ReadDashboard permission."
        },
    },
)


@router.get(
    "/dashboard",
    response_model=StudentDashboardResponse,
    operation_id="get_student_dashboard",
)
async def get_student_dashboard(
    response: Response,
    semester: str | None = Query(None, description="Target semester name"),
    year: str | None = Query(None, description="Target academic year (YYYY-YYYY)"),
    announcement_limit: int = Query(DEFAULT_LIMIT, alias="announcementLimit"),
    event_limit: int = Query(DEFAULT_LIMIT, alias="eventLimit"),
    activity_limit: int = Query(DEFAULT_LIMIT, alias="activityLimit"),
    user: CurrentUser = Depends(require_student_session),
    db: AsyncSession = Depends(get_db),
) -> StudentDashboardResponse:
    """Return single aggregate dashboard for the authenticated student (US-101)."""
    response.headers["Cache-Control"] = "private, no-store"
    return await service.get_dashboard(
        db,
        user=user,
        semester=semester,
        year=year,
        announcement_limit=announcement_limit,
        event_limit=event_limit,
        activity_limit=activity_limit,
    )
