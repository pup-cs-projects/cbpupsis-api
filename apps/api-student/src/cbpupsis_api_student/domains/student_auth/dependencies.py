"""Require a live student session on student-only endpoints."""

from __future__ import annotations

from fastapi import Depends

from cbpupsis_shared.domains.auth.constants import ROLE_STUDENT
from cbpupsis_shared.domains.auth.dependencies import CurrentUser, get_current_user
from cbpupsis_shared.domains.auth.exceptions import InsufficientRoleError


async def require_student_session(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """The shared dependency already enforces the idle timeout for every role;
    this adds only that the session was opened by the student sign-in."""
    if user.role != ROLE_STUDENT:
        raise InsufficientRoleError
    return user
