"""HTTP boundary for Admin and Superadmin credential and MFA sign-in."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.admin_auth import service
from cbpupsis_api_admin.domains.admin_auth.constants import (
    ADMIN_SESSION_COOKIE,
    ADMIN_SESSION_COOKIE_PATH,
)
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
    SuperadminEnrollmentRead,
    SuperadminLoginChallenge,
    SuperadminSessionRead,
    SuperadminTokenPair,
    TotpEnrollmentConfirmRequest,
    TotpEnrollmentRead,
    TotpEnrollmentRequest,
    WebAuthnEnrollmentConfirmRequest,
    WebAuthnEnrollmentRead,
    WebAuthnEnrollmentRequest,
)
from cbpupsis_core.config import settings
from cbpupsis_core.middleware import client_ip_or_none, rate_limit
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.constants import ROLE_ADMIN, ROLE_SUPERADMIN
from cbpupsis_shared.domains.auth.dependencies import (
    CurrentUser,
    clear_refresh_cookie,
    refresh_credentials,
    require_superadmin_session,
    set_refresh_cookie,
)

router = APIRouter()
protected_router = APIRouter()
superadmin_auth_router = APIRouter()
superadmin_protected_router = APIRouter()


def _device(request: Request) -> str:
    return request.headers.get("user-agent", "")


@superadmin_auth_router.post("/login", response_model=SuperadminLoginChallenge)
@rate_limit("login")
async def login_superadmin(
    request: Request, data: AdminLoginRequest, db: AsyncSession = Depends(get_db)
) -> SuperadminLoginChallenge:
    """Verify a Superadmin password; returns only an MFA challenge."""
    return await service.login_superadmin(db, email=data.email, password=data.password)


@superadmin_auth_router.post(
    "/mfa/totp/enroll", response_model=SuperadminEnrollmentRead
)
async def enroll_superadmin(
    data: TotpEnrollmentRequest, db: AsyncSession = Depends(get_db)
) -> SuperadminEnrollmentRead:
    return await service.enroll_superadmin_totp(
        db, challenge_token=data.challenge_token
    )


@superadmin_auth_router.post("/mfa/totp/confirm", response_model=SuperadminTokenPair)
async def confirm_superadmin(
    request: Request,
    response: Response,
    data: TotpEnrollmentConfirmRequest,
    db: AsyncSession = Depends(get_db),
) -> SuperadminTokenPair:
    pair = await service.verify_superadmin_totp(
        db,
        challenge_token=data.challenge_token,
        code=data.code,
        enrollment=True,
        ip_address=client_ip_or_none(request),
        device_info=_device(request),
    )
    set_refresh_cookie(response, pair.refresh_token, role=ROLE_SUPERADMIN)
    return pair


@superadmin_auth_router.post("/mfa/verify", response_model=SuperadminTokenPair)
async def verify_superadmin(
    request: Request,
    response: Response,
    data: TotpEnrollmentConfirmRequest,
    db: AsyncSession = Depends(get_db),
) -> SuperadminTokenPair:
    pair = await service.verify_superadmin_totp(
        db,
        challenge_token=data.challenge_token,
        code=data.code,
        enrollment=False,
        ip_address=client_ip_or_none(request),
        device_info=_device(request),
    )
    set_refresh_cookie(response, pair.refresh_token, role=ROLE_SUPERADMIN)
    return pair


@superadmin_auth_router.post("/refresh", response_model=SuperadminTokenPair)
async def refresh_superadmin(
    response: Response,
    data: RefreshRequest = Depends(refresh_credentials),
    db: AsyncSession = Depends(get_db),
) -> SuperadminTokenPair:
    pair = await service.refresh_superadmin(db, data)
    set_refresh_cookie(response, pair.refresh_token, role=ROLE_SUPERADMIN)
    return pair


@superadmin_auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout_superadmin(
    data: RefreshRequest, response: Response, db: AsyncSession = Depends(get_db)
) -> None:
    await service.logout_superadmin(db, data)
    clear_refresh_cookie(response, role=ROLE_SUPERADMIN)


@superadmin_protected_router.get("/me", response_model=SuperadminSessionRead)
async def superadmin_me(
    user: CurrentUser = Depends(require_superadmin_session),
) -> SuperadminSessionRead:
    return SuperadminSessionRead(id=user.id, email=user.email)


def _set_admin_cookies(response: Response, pair: AdminTokenPair) -> None:
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE,
        value=pair.access_token,
        max_age=settings.access_token_ttl_minutes * 60,
        path=ADMIN_SESSION_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="lax",
    )
    set_refresh_cookie(response, pair.refresh_token, role=ROLE_ADMIN)


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
    request: Request,
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
        ip_address=client_ip_or_none(request),
        device_info=_device(request),
    )
    _set_admin_cookies(response, pair)
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
    request: Request,
    data: TotpEnrollmentConfirmRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AdminTokenPair:
    """Prove the enrolled seed and issue the first Admin session."""
    pair = await service.confirm_totp_enrollment(
        db,
        challenge_token=data.challenge_token,
        code=data.code,
        ip_address=client_ip_or_none(request),
        device_info=_device(request),
    )
    _set_admin_cookies(response, pair)
    return pair


@router.post("/mfa/webauthn/enroll", response_model=WebAuthnEnrollmentRead)
async def enroll_webauthn(
    data: WebAuthnEnrollmentRequest,
    db: AsyncSession = Depends(get_db),
) -> WebAuthnEnrollmentRead:
    """Start first-time hardware-key registration after the password step."""
    return await service.begin_webauthn_enrollment(
        db, challenge_token=data.challenge_token
    )


@router.post("/mfa/webauthn/confirm", response_model=AdminTokenPair)
async def confirm_webauthn(
    request: Request,
    data: WebAuthnEnrollmentConfirmRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AdminTokenPair:
    """Verify registration and issue the first Admin session."""
    pair = await service.confirm_webauthn_enrollment(
        db,
        challenge_token=data.challenge_token,
        credential=data.credential,
        ip_address=client_ip_or_none(request),
        device_info=_device(request),
    )
    _set_admin_cookies(response, pair)
    return pair


@router.post("/refresh", response_model=AdminTokenPair)
async def refresh_admin(
    response: Response,
    data: RefreshRequest = Depends(refresh_credentials),
    db: AsyncSession = Depends(get_db),
) -> AdminTokenPair:
    """Rotate an Admin session and renew its cookies."""
    pair = await service.refresh_admin(db, data)
    _set_admin_cookies(response, pair)
    return pair


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout_admin(
    data: RefreshRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> None:
    """End the session, revoke the refresh token, and clear the cookies."""
    await service.logout_admin(db, data)
    response.delete_cookie(
        ADMIN_SESSION_COOKIE,
        path=ADMIN_SESSION_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="lax",
    )
    clear_refresh_cookie(response, role=ROLE_ADMIN)


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
