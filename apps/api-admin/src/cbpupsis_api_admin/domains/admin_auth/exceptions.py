"""Stable errors returned by the Admin authentication flow."""

from __future__ import annotations

from cbpupsis_core.exceptions import (
    AppError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
)


class AdminAuthError(AppError):
    """Base error for administrative authentication."""


class InvalidMfaError(AdminAuthError, UnauthorizedError):
    code = "AUTH_MFA_INVALID"

    def __init__(self) -> None:
        super().__init__("The security code or hardware-key response is invalid")


class MfaCodeReusedError(AdminAuthError, UnauthorizedError):
    code = "AUTH_MFA_CODE_REUSED"

    def __init__(self) -> None:
        super().__init__(
            "That security code has already been used; enter the current code"
        )


class MfaEnrollmentRequiredError(AdminAuthError, ForbiddenError):
    code = "AUTH_MFA_ENROLLMENT_REQUIRED"

    def __init__(self) -> None:
        super().__init__("Set up multi-factor authentication to continue")


class MfaRequiredError(AdminAuthError, UnauthorizedError):
    code = "AUTH_MFA_REQUIRED"

    def __init__(self) -> None:
        super().__init__("A second factor is required")


class AccountLockedError(AdminAuthError):
    status_code = 423
    code = "AUTH_ACCOUNT_LOCKED"

    def __init__(self, retry_after_seconds: int) -> None:
        retry_after = max(1, retry_after_seconds)
        super().__init__(
            "Account temporarily locked after repeated sign-in failures",
            response_fields={"retry_after_seconds": retry_after},
            headers={"Retry-After": str(retry_after)},
        )


class AdminProfileRequiredError(AdminAuthError, ForbiddenError):
    code = "AUTH_ADMIN_PROFILE_REQUIRED"

    def __init__(self) -> None:
        super().__init__("An administrator position must be assigned before sign-in")


class InsufficientAdminRoleError(AdminAuthError, ForbiddenError):
    code = "AUTH_INSUFFICIENT_ROLE"

    def __init__(self) -> None:
        super().__init__("This action is not available to your role")


class ScopedResourceNotFoundError(AdminAuthError, NotFoundError):
    code = "RESOURCE_NOT_FOUND"

    def __init__(self) -> None:
        super().__init__("Resource not found")
