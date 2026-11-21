"""Require a live, MFA-completed faculty session on faculty-only endpoints.

A challenge token never satisfies this: it is not an access token, so the
shared ``get_current_user`` refuses it before any role check runs.
"""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.constants import ROLE_FACULTY
from cbpupsis_shared.domains.auth.dependencies import CurrentUser, get_current_user
from cbpupsis_shared.domains.auth.exceptions import InsufficientRoleError
from cbpupsis_shared.domains.users import client as users_client


async def require_faculty_session(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """The shared dependency enforces the session and idle timeout; this adds
    that the session came from faculty sign-in with MFA and the profile is live."""
    if user.role != ROLE_FACULTY or user.claims.get("mfa") is not True:
        raise InsufficientRoleError
    profile = await users_client.get_faculty_profile(db, user.id)
    if profile is None or not profile.is_active:
        raise InsufficientRoleError
    return user
