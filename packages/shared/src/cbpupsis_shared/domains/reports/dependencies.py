"""Pre-handler authorization and caller scope resolution for the reports domain."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth import client as auth_client
from cbpupsis_shared.domains.auth.constants import ROLE_ADMIN, ROLE_SUPERADMIN
from cbpupsis_shared.domains.auth.exceptions import (
    InactiveUserError,
    InvalidAuthTokenError,
    MfaEnrollmentRequiredError,
    MfaRequiredError,
    NotAuthenticatedError,
)
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.reports.constants import READ_ENROLLMENT_REPORT
from cbpupsis_shared.domains.reports.exceptions import InsufficientRoleError
from cbpupsis_shared.domains.users import client as users_client
from cbpupsis_shared.domains.users.constants import AdminPosition

logger = logging.getLogger(__name__)
_bearer = HTTPBearer(auto_error=False)
ADMIN_SESSION_COOKIE = "admin_session"


@dataclass(frozen=True)
class ReportScope:
    """The caller's data scope: department, college, or all."""

    position: str
    department_id: str | None = None
    college_id: str | None = None
    is_all: bool = False


async def require_report_scope(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> ReportScope:
    """Require an MFA-completed session holding ReadEnrollmentReport within role scope.

    Finance accounts and unauthorized roles receive 403 AUTH_INSUFFICIENT_ROLE.
    """
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

    role = claims.get("role")
    if role not in (ROLE_ADMIN, ROLE_SUPERADMIN) or claims.get("mfa") is not True:
        raise InsufficientRoleError

    await auth_client.check_session(db, claims)

    granted = await iam_service.get_effective_permissions(db, user_id)
    if READ_ENROLLMENT_REPORT not in granted and role != ROLE_SUPERADMIN:
        raise InsufficientRoleError

    profile = await users_client.get_admin_profile(db, user_id)
    if profile is None:
        if role == ROLE_SUPERADMIN:
            return ReportScope(position="superadmin", is_all=True)
        raise InsufficientRoleError

    position = profile.position
    if position in (AdminPosition.finance, "finance"):
        raise InsufficientRoleError

    if position in (AdminPosition.registrar, "registrar") or role == ROLE_SUPERADMIN:
        return ReportScope(position=position, is_all=True)

    if position in (AdminPosition.dean, "dean"):
        return ReportScope(
            position=position,
            college_id=profile.college,
            is_all=False,
        )

    if position in (AdminPosition.chairperson, "chairperson", "chair"):
        return ReportScope(
            position=position,
            department_id=profile.department,
            is_all=False,
        )

    raise InsufficientRoleError
