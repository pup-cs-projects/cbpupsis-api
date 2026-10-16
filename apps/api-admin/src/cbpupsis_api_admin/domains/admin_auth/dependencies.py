"""Pre-handler Admin session and position-scope enforcement."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.admin_auth.constants import ADMIN_SESSION_COOKIE
from cbpupsis_api_admin.domains.admin_auth.exceptions import AdminProfileRequiredError
from cbpupsis_core.exceptions import AppError
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth import client as auth_client
from cbpupsis_shared.domains.auth.constants import ROLE_ADMIN, ROLE_SUPERADMIN
from cbpupsis_shared.domains.auth.dependencies import get_current_user
from cbpupsis_shared.domains.auth.exceptions import (
    InactiveUserError,
    InsufficientRoleError,
    InvalidAuthTokenError,
    MfaEnrollmentRequiredError,
    MfaRequiredError,
    NotAuthenticatedError,
    ResourceNotFoundError,
)
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP, SUPERADMIN_GROUP
from cbpupsis_shared.domains.users import client as users_client
from cbpupsis_shared.domains.users.constants import (
    INSTITUTION_WIDE_POSITIONS,
    AdminPosition,
)

logger = logging.getLogger(__name__)
_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AdminSession:
    """A live MFA-completed Admin principal with query-ready scope claims."""

    id: uuid.UUID
    email: str
    position: str
    department_id: str | None
    college_id: str | None
    role: str = ROLE_ADMIN

    def require_resource_scope(
        self, *, department_id: str | None, college_id: str | None
    ) -> None:
        """Hide records outside the position's department or college reach."""
        allowed = (
            self.role == ROLE_SUPERADMIN
            or self.position in INSTITUTION_WIDE_POSITIONS
            or (
                self.position == "dean"
                and self.college_id is not None
                and self.college_id == college_id
            )
            or (
                self.position == "chairperson"
                and self.department_id is not None
                and self.department_id == department_id
            )
        )
        if not allowed:
            raise ResourceNotFoundError


async def require_admin_session(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> AdminSession:
    """Require a live Admin session, completed MFA, and a valid position scope."""
    try:
        return await _admin_session(request, credentials, db)
    except AppError as error:
        # Logged with status and code only, never the token or identity.
        _log_refusal(request, error)
        raise


async def _admin_session(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    db: AsyncSession,
) -> AdminSession:
    token = (
        credentials.credentials
        if credentials is not None
        else request.cookies.get(ADMIN_SESSION_COOKIE)
    )
    if token is None:
        raise NotAuthenticatedError
    try:
        claims = auth_client.decode_access_session(token)
    except InvalidAuthTokenError as access_error:
        try:
            challenge = auth_client.decode_challenge_token(token, role=ROLE_ADMIN)
        except InvalidAuthTokenError:
            raise access_error from None
        if challenge.get("enrollment_required") is True:
            raise MfaEnrollmentRequiredError from access_error
        raise MfaRequiredError from access_error

    user_id = uuid.UUID(str(claims["sub"]))
    user = await users_client.get_active_user(db, user_id)
    if user is None:
        raise InactiveUserError
    if (
        claims.get("role") != ROLE_ADMIN
        or claims.get("mfa") is not True
        or not await iam_service.is_user_in_group(db, user_id, ADMIN_GROUP)
        or await iam_service.is_user_in_group(db, user_id, SUPERADMIN_GROUP)
    ):
        raise InsufficientRoleError
    await auth_client.check_session(db, claims)

    profile = await users_client.get_admin_profile(db, user_id)
    if profile is None or not profile.is_active:
        raise AdminProfileRequiredError
    position, department_id, college_id = (
        profile.position,
        profile.department,
        profile.college,
    )
    if (
        position not in set(AdminPosition)
        or (position == "chairperson" and not department_id)
        or (position == "dean" and not college_id)
    ):
        raise AdminProfileRequiredError
    if (
        claims.get("position") != position
        or claims.get("department_id") != department_id
        or claims.get("college_id") != college_id
    ):
        raise InvalidAuthTokenError

    request.state.user_id = str(user_id)
    return AdminSession(
        id=user_id,
        email=user.email,
        position=position,
        department_id=department_id,
        college_id=college_id,
    )


def _log_refusal(request: Request, error: Exception) -> None:
    logger.warning(
        "admin.authorization_refused",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": getattr(error, "status_code", 500),
            "error_code": getattr(error, "code", None),
        },
    )


async def require_admin_or_superadmin_session(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> AdminSession:
    if credentials is not None:
        try:
            user = await get_current_user(request, credentials, db)
        except InvalidAuthTokenError:
            return await require_admin_session(request, credentials, db)
        if user.role == ROLE_SUPERADMIN:
            return AdminSession(
                id=user.id,
                email=user.email,
                position=ROLE_SUPERADMIN,
                department_id=None,
                college_id=None,
                role=ROLE_SUPERADMIN,
            )
    return await require_admin_session(request, credentials, db)
