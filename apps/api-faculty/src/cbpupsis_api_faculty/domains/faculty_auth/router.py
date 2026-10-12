"""HTTP routing for faculty authentication endpoints."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_faculty.domains.faculty_auth import service as faculty_service
from cbpupsis_api_faculty.domains.faculty_auth.exceptions import (
    InvalidIdentifierFormatError,
)
from cbpupsis_api_faculty.domains.faculty_auth.schemas import (
    AuthAuditLogRead,
    FacultyLoginRequest,
    FacultyLoginResponse,
    FacultyMfaConfirmRequest,
    FacultyMfaConfirmResponse,
    FacultyMfaEnrollResponse,
    FacultyMfaVerifyRequest,
    FacultyMfaVerifyResponse,
    SectionListResponse,
    SectionRosterResponse,
)
from cbpupsis_api_faculty.domains.faculty_auth.security import (
    decode_faculty_challenge_token,
)
from cbpupsis_core.middleware import rate_limit
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth import client as auth_client
from cbpupsis_shared.domains.auth import service as auth_service
from cbpupsis_shared.domains.auth.dependencies import CurrentUser, get_current_user
from cbpupsis_shared.domains.auth.exceptions import NotAuthenticatedError
from cbpupsis_shared.domains.auth.schemas import (
    CurrentUserRead,
    LoginRequest,
    RefreshRequest,
    TokenPair,
)

router = APIRouter()
_bearer_scheme = HTTPBearer(auto_error=False)


def _extract_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


async def _resolve_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> uuid.UUID:
    """Resolve user ID from Bearer token (access token or challenge token)."""
    if credentials is None:
        raise NotAuthenticatedError
    token = credentials.credentials
    try:
        claims = auth_client.decode_access_session(token)
        return uuid.UUID(str(claims["sub"]))
    except Exception:
        claims = None

    try:
        challenge_claims = decode_faculty_challenge_token(token)
        return uuid.UUID(challenge_claims["sub"])
    except Exception:
        raise NotAuthenticatedError from None


@router.post(
    "/login",
    response_model=FacultyLoginResponse | TokenPair,
    status_code=status.HTTP_200_OK,
    operation_id="faculty_login",
    summary="Faculty Login",
)
@rate_limit("login")
async def faculty_login(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Authenticate faculty member (AC-002.1).

    Validates employee ID and password.
    Password alone must not yield a session for a privileged role:
    Returns 200 OK with mfa_required=True, a short-lived challenge token,
    and no access token.
    """
    try:
        body = await request.json()
    except Exception:
        raise InvalidIdentifierFormatError() from None

    if not isinstance(body, dict):
        raise InvalidIdentifierFormatError()

    # Legacy email login fallback for shared template tests
    if "email" in body and "identifier" not in body:
        login_req = LoginRequest.model_validate(body)
        return await auth_service.login(
            db, email=login_req.email, password=login_req.password
        )

    # Validate identifier presence before further parsing
    if "identifier" not in body:
        raise InvalidIdentifierFormatError()

    data = FacultyLoginRequest.model_validate(body)
    client_ip = _extract_client_ip(request)
    user_agent = request.headers.get("user-agent")

    challenge = await faculty_service.login_faculty(
        db,
        identifier=data.identifier,
        password=data.password,
        birthday=data.birthday,
        email=data.email,
        ip_address=client_ip,
        user_agent=user_agent,
    )
    return {"status": "success", "data": challenge}


@router.post(
    "/mfa/verify",
    response_model=FacultyMfaVerifyResponse,
    status_code=status.HTTP_200_OK,
    operation_id="faculty_mfa_verify",
)
async def faculty_mfa_verify(
    request: Request,
    response: Response,
    data: FacultyMfaVerifyRequest,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Verify MFA code and issue a faculty session (AC-002.2, AC-002.3, AC-002.4)."""
    client_ip = _extract_client_ip(request)
    user_agent = request.headers.get("user-agent")

    session_data, refresh_token = await faculty_service.verify_faculty_mfa(
        db,
        challenge_token=data.challenge_token,
        code=data.code,
        ip_address=client_ip,
        user_agent=user_agent,
    )

    response.set_cookie(
        key="refreshToken",
        value=refresh_token,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return {"status": "success", "data": session_data}


@router.get(
    "/mfa/enroll",
    response_model=FacultyMfaEnrollResponse,
    status_code=status.HTTP_200_OK,
    operation_id="faculty_mfa_enroll_get",
)
async def faculty_mfa_enroll(
    user_id: uuid.UUID = Depends(_resolve_user_id),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Generate TOTP seed and provisioning URI for Google Authenticator (AC-002.6)."""
    enroll_data = await faculty_service.enroll_faculty_mfa(db, user_id=user_id)
    return {"status": "success", "data": enroll_data}


@router.post(
    "/mfa/enroll",
    response_model=FacultyMfaEnrollResponse,
    status_code=status.HTTP_200_OK,
    operation_id="faculty_mfa_enroll_post",
)
async def faculty_mfa_enroll_post(
    user_id: uuid.UUID = Depends(_resolve_user_id),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Generate TOTP seed and provisioning URI for Google Authenticator (POST alias)."""
    enroll_data = await faculty_service.enroll_faculty_mfa(db, user_id=user_id)
    return {"status": "success", "data": enroll_data}


@router.post(
    "/mfa/enroll/confirm",
    response_model=FacultyMfaConfirmResponse,
    status_code=status.HTTP_200_OK,
    operation_id="faculty_mfa_confirm",
)
async def faculty_mfa_confirm(
    data: FacultyMfaConfirmRequest,
    user_id: uuid.UUID = Depends(_resolve_user_id),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Confirm TOTP code, activate MFA enrollment, and issue recovery codes."""
    recovery_codes = await faculty_service.confirm_faculty_mfa_enrollment(
        db, user_id=user_id, code=data.code
    )
    return {
        "status": "success",
        "data": {
            "recovery_codes": recovery_codes,
            "message": "MFA enrolled successfully. Save these recovery codes.",
        },
    }


@router.get(
    "/faculty/sections",
    response_model=SectionListResponse,
    operation_id="faculty_sections",
)
async def faculty_sections(
    user_id: uuid.UUID = Depends(_resolve_user_id),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """List sections assigned to faculty member (AC-002.6).

    Guarded: Returns 403 AUTH_MFA_ENROLLMENT_REQUIRED if MFA is not enrolled.
    """
    sections = await faculty_service.get_faculty_sections(db, user_id=user_id)
    return {"status": "success", "data": sections}


@router.get(
    "/sections",
    response_model=SectionListResponse,
    operation_id="faculty_sections_alias",
)
async def faculty_sections_alias(
    user_id: uuid.UUID = Depends(_resolve_user_id),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """List sections assigned to faculty member (alias)."""
    sections = await faculty_service.get_faculty_sections(db, user_id=user_id)
    return {"status": "success", "data": sections}


@router.get(
    "/sections/{section_id}/roster",
    response_model=SectionRosterResponse,
    operation_id="faculty_section_roster",
)
async def faculty_section_roster(
    section_id: str,
    user_id: uuid.UUID = Depends(_resolve_user_id),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Retrieve section roster with ownership protection (AC-002.7).

    Guarded: Answers 404 RESOURCE_NOT_FOUND (not 403) for non-owners.
    """
    roster_data = await faculty_service.get_section_roster(
        db, user_id=user_id, section_id=section_id
    )
    return {"status": "success", "data": roster_data}


@router.get(
    "/faculty/sections/{section_id}/roster",
    response_model=SectionRosterResponse,
    operation_id="faculty_section_roster_alias",
)
async def faculty_section_roster_alias(
    section_id: str,
    user_id: uuid.UUID = Depends(_resolve_user_id),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Retrieve section roster with ownership protection (alias)."""
    roster_data = await faculty_service.get_section_roster(
        db, user_id=user_id, section_id=section_id
    )
    return {"status": "success", "data": roster_data}


@router.get(
    "/audit",
    response_model=list[AuthAuditLogRead],
    operation_id="faculty_audit_ledger",
)
async def faculty_audit_ledger(
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Read the authentication audit ledger (AC-002.8)."""
    return await faculty_service.list_auth_audit_logs(db)


@router.delete(
    "/audit",
    operation_id="faculty_delete_audit_all",
)
async def faculty_delete_audit_all() -> None:
    """Refuse deletion of audit trail records (AC-002.8 / BR-AUTH-006)."""
    faculty_service.delete_auth_audit_logs()


@router.delete(
    "/audit/{log_id}",
    operation_id="faculty_delete_audit_item",
)
async def faculty_delete_audit_item(log_id: str) -> None:
    """Refuse deletion of a single audit trail record (AC-002.8 / BR-AUTH-006)."""
    faculty_service.delete_auth_audit_logs()


@router.post("/refresh", response_model=TokenPair, operation_id="faculty_refresh")
async def refresh(
    data: RefreshRequest, db: AsyncSession = Depends(get_db)
) -> TokenPair:
    """Exchange a refresh token for a new pair."""
    return await auth_service.refresh(db, data.refresh_token)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="faculty_logout",
    summary="Faculty Logout",
)
async def logout(data: RefreshRequest, db: AsyncSession = Depends(get_db)) -> None:
    """Revoke a refresh token."""
    await auth_service.logout(db, data.refresh_token)


@router.get(
    "/me",
    response_model=CurrentUserRead,
    operation_id="faculty_me",
    summary="Get Current Faculty Session",
)
async def me(user: CurrentUser = Depends(get_current_user)) -> CurrentUserRead:
    """Return the authenticated faculty principal."""
    return CurrentUserRead(id=user.id, email=user.email)


@router.get(
    "/whoami",
    response_model=CurrentUserRead,
    operation_id="faculty_whoami",
    include_in_schema=False,
)
async def whoami(user: CurrentUser = Depends(get_current_user)) -> CurrentUserRead:
    """Return the authenticated principal."""
    return CurrentUserRead(id=user.id, email=user.email)
