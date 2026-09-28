"""Pre-handler Admin session and position-scope enforcement."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.admin_auth import repository
from cbpupsis_api_admin.domains.admin_auth.exceptions import (
    AdminProfileRequiredError,
    InsufficientAdminRoleError,
    MfaEnrollmentRequiredError,
    MfaRequiredError,
    ScopedResourceNotFoundError,
)
from cbpupsis_api_admin.domains.admin_auth.security import decode_challenge_token
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth import client as auth_client
from cbpupsis_shared.domains.auth.exceptions import (
    InactiveUserError,
    InvalidAuthTokenError,
    NotAuthenticatedError,
)
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP, SUPERADMIN_GROUP
from cbpupsis_shared.domains.users import service as users_service

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

    def require_resource_scope(
        self, *, department_id: str | None, college_id: str | None
    ) -> None:
        """Hide records outside the position's department or college reach."""
        allowed = (
            self.position == "registrar"
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
            raise ScopedResourceNotFoundError


async def require_admin_session(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> AdminSession:
    """Require a current Admin role, completed MFA, and a valid position scope."""
    token = (
        credentials.credentials
        if credentials is not None
        else request.cookies.get("admin_session")
    )
    if token is None:
        error = NotAuthenticatedError()
        _log_refusal(request, error)
        raise error

    try:
        claims = auth_client.decode_access_session(token)
    except InvalidAuthTokenError as access_error:
        try:
            challenge = decode_challenge_token(token)
        except InvalidAuthTokenError:
            _log_refusal(request, access_error)
            raise access_error from None
        error = (
            MfaEnrollmentRequiredError()
            if challenge.get("enrollment_required") is True
            else MfaRequiredError()
        )
        _log_refusal(request, error)
        raise error from access_error

    user_id = uuid.UUID(str(claims["sub"]))
    user = await users_service.get_active_user(db, user_id)
    if user is None:
        error = InactiveUserError()
        _log_refusal(request, error)
        raise error
    if (
        claims.get("role") != "admin"
        or claims.get("mfa") is not True
        or not await iam_service.is_user_in_group(db, user_id, ADMIN_GROUP)
        or await iam_service.is_user_in_group(db, user_id, SUPERADMIN_GROUP)
    ):
        error = InsufficientAdminRoleError()
        _log_refusal(request, error)
        raise error

    profile = await repository.get_admin_profile(db, user_id)
    if profile is None or not profile.is_active:
        error = AdminProfileRequiredError()
        _log_refusal(request, error)
        raise error

    position = profile.position
    department_id = profile.department
    college_id = profile.college
    invalid_scope = (
        position not in {"chairperson", "dean", "registrar"}
        or (position == "chairperson" and not department_id)
        or (position == "dean" and not college_id)
    )
    if invalid_scope:
        error = AdminProfileRequiredError()
        _log_refusal(request, error)
        raise error
    if (
        claims.get("position") != position
        or claims.get("department_id") != department_id
        or claims.get("college_id") != college_id
    ):
        error = InvalidAuthTokenError()
        _log_refusal(request, error)
        raise error

    request.state.user_id = str(user_id)
    return AdminSession(
        id=user_id,
        email=user.email,
        position=str(position),
        department_id=str(department_id) if department_id is not None else None,
        college_id=str(college_id) if college_id is not None else None,
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
