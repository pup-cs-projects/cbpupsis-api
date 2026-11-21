"""HTTP routing for faculty sign-in and MFA, mounted at /api/v1/auth/faculty."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_faculty.domains.faculty_auth import service
from cbpupsis_api_faculty.domains.faculty_auth.dependencies import (
    require_faculty_session,
)
from cbpupsis_api_faculty.domains.faculty_auth.schemas import (
    ChallengeRequest,
    FacultyEnrollmentResult,
    FacultyLoginChallenge,
    FacultyLoginRequest,
    FacultyMfaVerifyRequest,
    FacultySessionRead,
    FacultyTokenPair,
    TotpConfirmRequest,
    TotpEnrollmentRead,
)
from cbpupsis_core.middleware import client_ip_or_none, rate_limit
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.constants import ROLE_FACULTY
from cbpupsis_shared.domains.auth.dependencies import (
    CurrentUser,
    clear_refresh_cookie,
    refresh_credentials,
    set_refresh_cookie,
)
from cbpupsis_shared.domains.auth.schemas import RefreshRequest
from cbpupsis_shared.domains.users import client as users_client

router = APIRouter()


def _device(request: Request) -> str:
    return request.headers.get("user-agent", "")


@router.post(
    "/login", response_model=FacultyLoginChallenge, operation_id="faculty_login"
)
@rate_limit("login")
async def faculty_login(
    request: Request,
    data: FacultyLoginRequest,
    db: AsyncSession = Depends(get_db),
) -> FacultyLoginChallenge:
    """Verify employee id and password; returns only an MFA challenge."""
    return await service.login_faculty(
        db,
        employee_id=data.employee_id,
        password=data.password,
        ip_address=client_ip_or_none(request),
        device_info=_device(request),
    )


@router.post(
    "/mfa/totp/enroll",
    response_model=TotpEnrollmentRead,
    operation_id="faculty_totp_enroll",
)
@rate_limit("login")
async def faculty_totp_enroll(
    request: Request, data: ChallengeRequest, db: AsyncSession = Depends(get_db)
) -> TotpEnrollmentRead:
    """Start authenticator enrollment for a faculty member not yet enrolled."""
    return await service.begin_totp_enrollment(db, challenge_token=data.challenge_token)


@router.post(
    "/mfa/totp/confirm",
    response_model=FacultyEnrollmentResult,
    operation_id="faculty_totp_confirm",
)
@rate_limit("login")
async def faculty_totp_confirm(
    request: Request,
    response: Response,
    data: TotpConfirmRequest,
    db: AsyncSession = Depends(get_db),
) -> FacultyEnrollmentResult:
    """Confirm the seed; returns the first session and one-time recovery codes."""
    result = await service.confirm_totp_enrollment(
        db,
        challenge_token=data.challenge_token,
        code=data.code,
        ip_address=client_ip_or_none(request),
        device_info=_device(request),
    )
    set_refresh_cookie(response, result.refresh_token, role=ROLE_FACULTY)
    return result


@router.post(
    "/mfa/verify", response_model=FacultyTokenPair, operation_id="faculty_mfa_verify"
)
@rate_limit("login")
async def faculty_mfa_verify(
    request: Request,
    response: Response,
    data: FacultyMfaVerifyRequest,
    db: AsyncSession = Depends(get_db),
) -> FacultyTokenPair:
    """Complete sign-in with a TOTP code or one recovery code."""
    pair = await service.verify_mfa(
        db,
        data,
        ip_address=client_ip_or_none(request),
        device_info=_device(request),
    )
    set_refresh_cookie(response, pair.refresh_token, role=ROLE_FACULTY)
    return pair


@router.post(
    "/refresh", response_model=FacultyTokenPair, operation_id="faculty_refresh"
)
async def faculty_refresh(
    response: Response,
    data: RefreshRequest = Depends(refresh_credentials),
    db: AsyncSession = Depends(get_db),
) -> FacultyTokenPair:
    """Rotate the faculty session's refresh token."""
    pair = await service.refresh_faculty(db, data)
    set_refresh_cookie(response, pair.refresh_token, role=ROLE_FACULTY)
    return pair


@router.post(
    "/logout", status_code=status.HTTP_204_NO_CONTENT, operation_id="faculty_logout"
)
async def faculty_logout(
    data: RefreshRequest, response: Response, db: AsyncSession = Depends(get_db)
) -> None:
    """End the session server-side and clear the refresh cookie."""
    await service.logout_faculty(db, data)
    clear_refresh_cookie(response, role=ROLE_FACULTY)


@router.get("/me", response_model=FacultySessionRead, operation_id="faculty_me")
async def faculty_me(
    user: CurrentUser = Depends(require_faculty_session),
    db: AsyncSession = Depends(get_db),
) -> FacultySessionRead:
    """Return the signed-in faculty member."""
    profile = await users_client.get_faculty_profile(db, user.id)
    return FacultySessionRead(
        id=user.id,
        email=user.email,
        employee_id=profile.employee_id if profile else None,
    )
