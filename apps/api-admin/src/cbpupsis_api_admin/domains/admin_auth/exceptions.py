"""Errors only the Admin sign-in raises.

The MFA, lockout, role, and scope errors every role shares are in
``cbpupsis_shared.domains.auth.exceptions``.
"""

from __future__ import annotations

from cbpupsis_core.exceptions import ForbiddenError
from cbpupsis_shared.domains.auth.exceptions import AuthError


class AdminAuthError(AuthError):
    """Base error for administrative authentication."""


class AdminProfileRequiredError(AdminAuthError, ForbiddenError):
    code = "AUTH_ADMIN_PROFILE_REQUIRED"

    def __init__(self) -> None:
        super().__init__("An administrator position must be assigned before sign-in")
