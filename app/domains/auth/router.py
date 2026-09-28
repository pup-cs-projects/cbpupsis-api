"""HTTP layer for the auth domain: register, login, refresh, logout, and the
email-verification and password-reset flows.

Thin by design — each endpoint parses the request, delegates to the service, and
shapes the response. All flow logic lives in ``app.domains.auth.service``.

Several endpoints here answer 204 unconditionally
(``/forgot-password``, ``/resend-verification``). That is a security property,
not laziness: a status that varied with whether the address exists would let
anyone test an email list against the user base.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.middleware import rate_limit
from app.database import get_db
from app.domains.auth import service as auth_service
from app.domains.auth.dependencies import CurrentUser, get_current_user
from app.domains.auth.schemas import (
    AdminLoginChallenge,
    AdminTokenPair,
    ChangePasswordRequest,
    CurrentUserRead,
    ForgotPasswordRequest,
    LoginRequest,
    MfaVerifyRequest,
    RefreshRequest,
    RegisterRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    TokenPair,
    TotpEnrollmentConfirmRequest,
    TotpEnrollmentRead,
    TotpEnrollmentRequest,
    VerifyEmailRequest,
)
from app.domains.users.schemas import UserRead

router = APIRouter()


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
@rate_limit("register")
async def register(
    request: Request,
    data: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> UserRead:
    """Create an account. Returns the new user; tokens come from /login.

    Rate limited per IP against signup spam. ``request`` is unused by the body
    but required: slowapi reads the limiter and the caller's key off it, and
    refuses to decorate an endpoint that does not declare it.
    """
    user = await auth_service.register(
        db, email=data.email, password=data.password, full_name=data.full_name
    )
    return UserRead.model_validate(user)


@router.post("/login", response_model=TokenPair)
@rate_limit("login")
async def login(
    request: Request,
    data: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenPair:
    """Exchange email and password for an access/refresh token pair.

    Rate limited against credential stuffing.

    Three outcomes, and the difference between them is deliberate:

    - **401** — unknown email, wrong password, or a disabled account, all with
      one identical message so the endpoint cannot be used to discover which
      addresses are registered.
    - **403** with ``code: "email_not_verified"`` — the password was correct but
      the address is unverified. Branch on the code, not the message, and send
      the user to a resend-verification screen rather than back to login.
    - **200** — a token pair.

    Administrative accounts use ``/admin/login`` instead. They are refused at
    this endpoint so a password can never create an admin session without MFA.
    """
    return await auth_service.login(db, email=data.email, password=data.password)


@router.post("/admin/login", response_model=AdminLoginChallenge)
@rate_limit("login")
async def login_admin(
    request: Request,
    data: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> AdminLoginChallenge:
    """Verify admin credentials and return a non-privileged MFA challenge."""
    return await auth_service.login_admin(db, email=data.email, password=data.password)


def _set_admin_cookie(response: Response, token: str) -> None:
    """Put the administrative bearer in a hardened API-scoped cookie."""
    response.set_cookie(
        key="admin_session",
        value=token,
        max_age=settings.access_token_ttl_minutes * 60,
        path="/api/v1",
        secure=True,
        httponly=True,
        samesite="lax",
    )


@router.post("/admin/mfa/verify", response_model=AdminTokenPair)
async def verify_mfa(
    data: MfaVerifyRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AdminTokenPair:
    """Complete an admin sign-in with TOTP or a WebAuthn hardware key."""
    pair = await auth_service.verify_mfa(
        db,
        challenge_token=data.challenge_token,
        code=data.code,
        assertion=data.assertion,
    )
    _set_admin_cookie(response, pair.access_token)
    return pair


@router.post("/admin/mfa/totp/enroll", response_model=TotpEnrollmentRead)
async def enroll_totp(
    data: TotpEnrollmentRequest,
    db: AsyncSession = Depends(get_db),
) -> TotpEnrollmentRead:
    """Start the only route reachable with an unenrolled admin challenge."""
    return await auth_service.begin_totp_enrollment(
        db, challenge_token=data.challenge_token
    )


@router.post("/admin/mfa/totp/confirm", response_model=AdminTokenPair)
async def confirm_totp(
    data: TotpEnrollmentConfirmRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AdminTokenPair:
    """Confirm TOTP enrollment and issue the first administrative session."""
    pair = await auth_service.confirm_totp_enrollment(
        db, challenge_token=data.challenge_token, code=data.code
    )
    _set_admin_cookie(response, pair.access_token)
    return pair


@router.post("/refresh", response_model=TokenPair | AdminTokenPair)
async def refresh(
    data: RefreshRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenPair | AdminTokenPair:
    """Exchange a refresh token for a new pair, invalidating the old token."""
    pair = await auth_service.refresh(db, data.refresh_token)
    if isinstance(pair, AdminTokenPair):
        _set_admin_cookie(response, pair.access_token)
    return pair


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    data: RefreshRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke a refresh token. Idempotent: always succeeds."""
    await auth_service.logout(db, data.refresh_token)
    response.delete_cookie(
        "admin_session",
        path="/api/v1",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    # Clear the path used by pre-release builds as well, so local upgrades do
    # not leave a second stale cookie competing with the current one.
    response.delete_cookie(
        "admin_session",
        path="/api/v1/admin",
        secure=True,
        httponly=True,
        samesite="lax",
    )


@router.get("/whoami", response_model=CurrentUserRead)
async def whoami(user: CurrentUser = Depends(get_current_user)) -> CurrentUserRead:
    """Return the authenticated principal — useful for verifying token wiring."""
    return CurrentUserRead(id=user.id, email=user.email)


@router.post("/verify-email", status_code=status.HTTP_204_NO_CONTENT)
async def verify_email(
    data: VerifyEmailRequest, db: AsyncSession = Depends(get_db)
) -> None:
    """Redeem an email-verification token.

    401 for a token that is unknown, expired, already used, or issued for a
    different purpose — all with the same message, so the response cannot be
    used to probe which.
    """
    await auth_service.verify_email(db, data.token)


@router.post("/resend-verification", status_code=status.HTTP_204_NO_CONTENT)
@rate_limit("resend_verification")
async def resend_verification(
    request: Request,
    data: ResendVerificationRequest,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Request a fresh verification email.

    Always 204, whether or not the address is registered, active, or already
    verified. Any live token previously issued to that user is invalidated.

    The strictest limit in the app, alongside /forgot-password: this endpoint
    sends mail to an address the caller names, so without one an anonymous
    caller can bomb a stranger's inbox from your domain and spend your SES
    budget doing it.
    """
    await auth_service.resend_verification(db, data.email)


@router.post("/forgot-password", status_code=status.HTTP_204_NO_CONTENT)
@rate_limit("forgot_password")
async def forgot_password(
    request: Request,
    data: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Request a password-reset email.

    Always 204. Returning 404 for an unknown address would make this endpoint a
    membership oracle for any email list.

    Rate limited as strictly as /resend-verification, and for the same reason:
    it mails an address chosen by an unauthenticated caller.
    """
    await auth_service.forgot_password(db, data.email)


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    data: ResetPasswordRequest, db: AsyncSession = Depends(get_db)
) -> None:
    """Redeem a reset token and set a new password.

    Every refresh token for the account is revoked, so any session an attacker
    holds dies with the reset. The client must log in again with the new
    password.
    """
    await auth_service.reset_password(db, data.token, data.new_password)


@router.post("/change-password", response_model=TokenPair)
async def change_password(
    data: ChangePasswordRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TokenPair:
    """Change your own password, returning a replacement token pair.

    Authorization is by identity alone: the target is the authenticated user,
    taken from the token, so there is no id to tamper with and no object-level
    check to make. Requires the current password.

    All existing refresh tokens — including the caller's — are revoked, and the
    returned pair replaces them.
    """
    return await auth_service.change_password(
        db,
        user_id=user.id,
        current_password=data.current_password,
        new_password=data.new_password,
    )
