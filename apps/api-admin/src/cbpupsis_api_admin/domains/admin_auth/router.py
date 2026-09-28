"""HTTP boundary for Admin credential and MFA sign-in."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.admin_auth import service
from cbpupsis_api_admin.domains.admin_auth.dependencies import (
    AdminSession,
    require_admin_session,
)
from cbpupsis_api_admin.domains.admin_auth.schemas import (
    AdminLoginChallenge,
    AdminLoginRequest,
    AdminSessionRead,
    AdminTokenPair,
    MfaVerifyRequest,
    RefreshRequest,
    TotpEnrollmentConfirmRequest,
    TotpEnrollmentRead,
    TotpEnrollmentRequest,
)
from cbpupsis_core.config import settings
from cbpupsis_core.middleware import rate_limit
from cbpupsis_database.session import get_db

router = APIRouter()
protected_router = APIRouter()


def _set_admin_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="admin_session",
        value=token,
        max_age=settings.access_token_ttl_minutes * 60,
        path="/api/v1/admin",
        secure=True,
        httponly=True,
        samesite="lax",
    )


@router.post("/login", response_model=AdminLoginChallenge)
@rate_limit("login")
async def login_admin(
    request: Request,
    data: AdminLoginRequest,
    db: AsyncSession = Depends(get_db),
) -> AdminLoginChallenge:
    """Verify credentials and issue no session until MFA completes."""
    return await service.login_admin(db, email=data.email, password=data.password)


@router.post("/mfa/verify", response_model=AdminTokenPair)
async def verify_mfa(
    data: MfaVerifyRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AdminTokenPair:
    """Complete sign-in using Google Authenticator TOTP or a hardware key."""
    pair = await service.verify_mfa(
        db,
        challenge_token=data.challenge_token,
        code=data.code,
        assertion=data.assertion,
    )
    _set_admin_cookie(response, pair.access_token)
    return pair


@router.post("/mfa/totp/enroll", response_model=TotpEnrollmentRead)
async def enroll_totp(
    data: TotpEnrollmentRequest,
    db: AsyncSession = Depends(get_db),
) -> TotpEnrollmentRead:
    """Return the standard otpauth URI Google Authenticator scans."""
    return await service.begin_totp_enrollment(db, challenge_token=data.challenge_token)


@router.post("/mfa/totp/confirm", response_model=AdminTokenPair)
async def confirm_totp(
    data: TotpEnrollmentConfirmRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AdminTokenPair:
    """Prove the enrolled seed and issue the first Admin session."""
    pair = await service.confirm_totp_enrollment(
        db, challenge_token=data.challenge_token, code=data.code
    )
    _set_admin_cookie(response, pair.access_token)
    return pair


@router.post("/refresh", response_model=AdminTokenPair)
async def refresh_admin(
    data: RefreshRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AdminTokenPair:
    """Rotate an Admin session and renew its hardened cookie."""
    pair = await service.refresh_admin(db, data)
    _set_admin_cookie(response, pair.access_token)
    return pair


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout_admin(
    data: RefreshRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke the refresh token and clear the Admin cookie."""
    await service.logout_admin(db, data)
    response.delete_cookie(
        "admin_session",
        path="/api/v1/admin",
        secure=True,
        httponly=True,
        samesite="lax",
    )


@protected_router.get("/me", response_model=AdminSessionRead)
async def read_admin_session(
    admin: AdminSession = Depends(require_admin_session),
) -> AdminSessionRead:
    """Return the MFA-completed Admin identity and query-ready data scope."""
    return AdminSessionRead(
        id=admin.id,
        email=admin.email,
        position=admin.position,
        department_id=admin.department_id,
        college_id=admin.college_id,
    )
