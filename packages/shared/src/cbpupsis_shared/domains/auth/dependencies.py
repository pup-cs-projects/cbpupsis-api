"""Authentication dependencies: turn a request into a known user.

The flow is: extract the bearer token, verify its signature and type, read the
``sub`` claim, then load the user row.

Authorization — what the user may *do* — is deliberately not handled here; that
lives in ``cbpupsis_shared.domains.iam.dependencies``. This module answers only "who is
this?", and the split is what lets permissions change without touching
authentication.

**Email verification does not gate login.** An unverified user authenticates
normally and gets a full token pair. Blocking login outright is a worse trade
than it looks: it strands every user whose verification mail bounced or landed
in spam, and it makes the login endpoint report whether an address is registered
*and* unverified. Instead, verification is enforced where it actually matters,
by declaring :func:`require_verified_email` on the specific endpoints that
should not run for an unproven address.

**A temporary password closes every route but one.** An account still holding
a system-issued password answers 403 ``PASSWORD_CHANGE_REQUIRED`` from
:func:`get_current_user` and every role guard built on the same session check.
Only :func:`get_password_changing_user`, declared by ``POST /auth/change-password``,
lets it through. The logout routes take a refresh token, not a session, so they
stay open without it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from fastapi import Body, Cookie, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.audit import client as audit_client
from cbpupsis_shared.domains.auth import service as auth_service
from cbpupsis_shared.domains.auth.constants import (
    REFRESH_COOKIE_NAME,
    ROLE_SUPERADMIN,
)
from cbpupsis_shared.domains.auth.exceptions import (
    AuthSessionExpiredError,
    InactiveUserError,
    InvalidAuthTokenError,
    NotAuthenticatedError,
    SuperadminRoleRequiredError,
    UnverifiedEmailError,
)
from cbpupsis_shared.domains.auth.schemas import RefreshRequest
from cbpupsis_shared.domains.auth.security import decode_token, require_session_version
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import SUPERADMIN_GROUP
from cbpupsis_shared.domains.users import service as users_service

# auto_error=False so a MISSING header reaches our own code instead of raising
# Starlette's 403. A missing credential is 401 ("who are you?"), not 403 ("I
# know who you are and you may not"); the default conflates the two.
bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    """The authenticated principal, as consumed by endpoints and guards.

    Deliberately minimal: the local user id plus identity attributes cheap to
    carry. Permissions are resolved separately, on demand, so they always
    reflect current state rather than state at token-issue time.
    """

    id: uuid.UUID
    email: str
    role: str | None = None
    session_id: uuid.UUID | None = None
    #: The verified access-token claims, for flows that must reissue the same
    #: kind of session (a password change). Never used for authorization.
    claims: Mapping[str, object] = field(default_factory=dict, compare=False)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """Resolve the current user from a verified access token and live session.

    The user row is loaded on every request rather than trusted from the token,
    so deactivating or deleting an account takes effect immediately instead of
    when the token happens to expire. Every role's token names a server-side
    session (``sid``) that must still be live and not idle past the timeout.
    A pending temporary password answers 403 ``PASSWORD_CHANGE_REQUIRED``.
    """
    return await _authenticate(request, credentials, db, allow_password_change=False)


async def get_password_changing_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """:func:`get_current_user`, still open while a temporary password is pending.

    Declared only by the password change, the one route such an account may
    call. Anything else that declared it would reopen the whole API to an
    account that has not yet chosen its own password.
    """
    return await _authenticate(request, credentials, db, allow_password_change=True)


async def _authenticate(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    db: AsyncSession,
    *,
    allow_password_change: bool,
) -> CurrentUser:
    db.info.pop("superadmin_audit", None)
    db.info.pop("audit_event_handler", None)
    if credentials is None:
        raise NotAuthenticatedError
    try:
        claims = decode_token(credentials.credentials, expected_type="access")
    except InvalidAuthTokenError as invalid:
        # A signed but expired JWT can identify an idle session for cleanup,
        # never authorize a request or extend its activity window.
        stale = decode_token(
            credentials.credentials, expected_type="access", allow_expired=True
        )
        if stale.get("role") == ROLE_SUPERADMIN:
            await auth_service.check_superadmin_session(
                db, stale, touch=False, allow_password_change=True
            )
            raise AuthSessionExpiredError from None
        if stale.get("sid"):
            # Raises AUTH_SESSION_EXPIRED (and deletes the row) if it went idle.
            await auth_service.check_session(
                db, stale, touch=False, allow_password_change=True
            )
        raise invalid from None
    user = await users_service.get_active_user(db, uuid.UUID(claims["sub"]))
    if user is None:
        raise InactiveUserError

    require_session_version(claims, user.session_version)
    role = claims.get("role")
    if role == ROLE_SUPERADMIN:
        sid = await auth_service.check_superadmin_session(
            db, claims, allow_password_change=allow_password_change
        )
        request.state.superadmin_audit = audit_client.begin_superadmin_request(
            db,
            user.id,
            request.method,
            getattr(request.scope.get("route"), "path", "private-record")
            if getattr(request.state, "private_response", False)
            else request.url.path,
        )
        db.info["audit_event_handler"] = audit_client.capture_event
        request.state.audit_db = db
    elif await iam_service.is_user_in_group(db, user.id, SUPERADMIN_GROUP):
        raise SuperadminRoleRequiredError
    else:
        sid = await auth_service.check_session(
            db, claims, allow_password_change=allow_password_change
        )

    # Published for the rate limiter, which keys authenticated callers by
    # identity rather than by address so users behind one NAT do not consume
    # each other's allowance. Set only after the token is verified and the
    # account confirmed live, so the value is never attacker-controlled.
    request.state.user_id = str(user.id)
    return CurrentUser(
        id=user.id,
        email=user.email,
        role=str(role) if role is not None else None,
        session_id=sid,
        claims=claims,
    )


async def require_superadmin_session(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if user.role != "superadmin":
        raise SuperadminRoleRequiredError
    return user


async def require_verified_email(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """Authenticate, and additionally require a verified email address.

    Declare this instead of :func:`get_current_user` on endpoints that should
    not run for an unproven address — anything that emails other people, spends
    money, or is expensive to undo::

        @router.post("/invitations")
        async def invite(user: CurrentUser = Depends(require_verified_email)):
            ...

    Raises :class:`ForbiddenError` (403), not 401: the caller *is* authenticated,
    and a 401 would tell a client to discard a perfectly good token and send the
    user back through login, which would not fix anything.

    The verification state is read from the database per request rather than
    from the token, so verifying takes effect on the next call instead of when
    the access token happens to expire.
    """
    record = await users_service.get_by_id(db, user.id)
    if record.email_verified_at is None:
        raise UnverifiedEmailError("Email address not verified")
    return user


def refresh_cookie_path(role: str) -> str:
    """Scope the cookie to one role's auth routes.

    Setting and clearing must name the same path, or the browser keeps the cookie.
    """
    return f"/api/v1/auth/{role}"


def set_refresh_cookie(response: Response, refresh_token: str, *, role: str) -> None:
    """Set the one refresh-token cookie shape every role uses.

    Whether clients keep the refresh token in this cookie or in the response
    body is an open decision (see the Sessions row of the CONVENTIONS gap
    table); both are sent until it closes.
    """
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=refresh_token,
        max_age=settings.refresh_token_ttl_days * 24 * 3600,
        path=refresh_cookie_path(role),
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_refresh_cookie(response: Response, *, role: str) -> None:
    """Delete the refresh-token cookie :func:`set_refresh_cookie` set."""
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path=refresh_cookie_path(role),
        secure=True,
        httponly=True,
        samesite="lax",
    )


async def refresh_credentials(
    data: RefreshRequest | None = Body(default=None),
    cookie_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
) -> RefreshRequest:
    """The refresh token a refresh call presents: from the body, else from the cookie.

    The cookie is what lets a web client restore its session after a page reload
    without keeping any token in script-readable storage: the browser sends it, and
    script never sees it. The body still wins when both are present, so existing
    clients are unchanged.
    """
    if data is not None:
        return data
    if cookie_token:
        return RefreshRequest(refresh_token=cookie_token)
    raise NotAuthenticatedError()
