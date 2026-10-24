"""HTTP routing for student sign-in, mounted at /api/v1/auth/student."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.student_auth import service
from cbpupsis_api_student.domains.student_auth.dependencies import (
    require_student_session,
)
from cbpupsis_api_student.domains.student_auth.schemas import (
    StudentLoginRequest,
    StudentSessionRead,
    StudentTokenPair,
)
from cbpupsis_core.middleware import client_ip_or_none, rate_limit
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.constants import ROLE_STUDENT
from cbpupsis_shared.domains.auth.dependencies import (
    CurrentUser,
    clear_refresh_cookie,
    refresh_credentials,
    set_refresh_cookie,
)
from cbpupsis_shared.domains.auth.schemas import RefreshRequest

router = APIRouter()


@router.post("/login", response_model=StudentTokenPair, operation_id="student_login")
@rate_limit("login")
async def student_login(
    request: Request,
    response: Response,
    data: StudentLoginRequest,
    db: AsyncSession = Depends(get_db),
) -> StudentTokenPair:
    """Sign in with student number, birthdate, and password (US-101)."""
    pair = await service.login_student(
        db,
        data,
        ip_address=client_ip_or_none(request),
        device_info=request.headers.get("user-agent", ""),
    )
    set_refresh_cookie(response, pair.refresh_token, role=ROLE_STUDENT)
    return pair


@router.post(
    "/refresh", response_model=StudentTokenPair, operation_id="student_refresh"
)
async def student_refresh(
    response: Response,
    data: RefreshRequest = Depends(refresh_credentials),
    db: AsyncSession = Depends(get_db),
) -> StudentTokenPair:
    """Rotate the student session's refresh token."""
    pair = await service.refresh_student(db, data)
    set_refresh_cookie(response, pair.refresh_token, role=ROLE_STUDENT)
    return pair


@router.post(
    "/logout", status_code=status.HTTP_204_NO_CONTENT, operation_id="student_logout"
)
async def student_logout(
    data: RefreshRequest, response: Response, db: AsyncSession = Depends(get_db)
) -> None:
    """End the session server-side and clear the refresh cookie."""
    await service.logout_student(db, data)
    clear_refresh_cookie(response, role=ROLE_STUDENT)


@router.get("/me", response_model=StudentSessionRead, operation_id="student_me")
async def student_me(
    user: CurrentUser = Depends(require_student_session),
) -> StudentSessionRead:
    """Return the signed-in student; 401 once the session has idled out."""
    return StudentSessionRead(id=user.id, email=user.email)
