"""Exceptions for the faculty authentication domain."""

from __future__ import annotations

from cbpupsis_core.exceptions import AppError


class InvalidIdentifierFormatError(AppError):
    """Raised when faculty identifier format is invalid (HTTP 400)."""

    status_code = 400
    code = "INVALID_INPUT"

    def __init__(
        self,
        detail: str = "Invalid identifier format. Expected format: YYYY-NNNNN-XX-N",
    ) -> None:
        super().__init__(detail=detail, code=self.code)


class FacultyAuthFailedError(AppError):
    """Raised when authentication credentials fail (HTTP 401).

    Preserves generic message to avoid credential enumeration (BR-AUTH-002).
    """

    status_code = 401
    code = "AUTH_FAILED"

    def __init__(
        self,
        remaining_attempts: int = 3,
        detail: str | None = None,
    ) -> None:
        if detail is None:
            detail = (
                f"Invalid ID or password. "
                f"{remaining_attempts} attempts remaining before lockout."
            )
        self.remaining_attempts = remaining_attempts
        super().__init__(detail=detail, code=self.code)


class FacultyAccountLockedError(AppError):
    """Raised when account is locked due to consecutive failures (HTTP 423)."""

    status_code = 423
    code = "AUTH_ACCOUNT_LOCKED"

    def __init__(
        self,
        retry_after_seconds: int = 900,
        detail: str = (
            "Account locked due to 5 consecutive failed attempts. "
            "Try again in 15 minutes."
        ),
        code: str = "AUTH_ACCOUNT_LOCKED",
    ) -> None:
        self.retry_after_seconds = retry_after_seconds
        self.data = {"retry_after_seconds": retry_after_seconds}
        self.code = code
        super().__init__(detail=detail, code=self.code)


class FacultyMfaInvalidError(AppError):
    """Raised when an invalid MFA verification code is submitted (HTTP 401)."""

    status_code = 401
    code = "AUTH_MFA_INVALID"

    def __init__(
        self,
        remaining_attempts: int | None = None,
        detail: str | None = None,
    ) -> None:
        if detail is None:
            if remaining_attempts is not None:
                detail = (
                    f"Invalid authentication code. "
                    f"{remaining_attempts} attempts remaining before lockout."
                )
            else:
                detail = "Invalid authentication code."
        self.remaining_attempts = remaining_attempts
        super().__init__(detail=detail, code=self.code)


class FacultyMfaCodeReusedError(AppError):
    """Raised when a spent MFA code is resubmitted within validity window (HTTP 401)."""

    status_code = 401
    code = "AUTH_MFA_CODE_REUSED"

    def __init__(
        self,
        detail: str = (
            "This code has already been used. Please wait for the next code."
        ),
    ) -> None:
        super().__init__(detail=detail, code=self.code)


class FacultyMfaExpiredError(AppError):
    """Raised when an MFA code from a previous window is submitted (HTTP 401)."""

    status_code = 401
    code = "AUTH_MFA_INVALID"

    def __init__(
        self,
        detail: str = (
            "The code has expired. Please enter the current code from your "
            "authenticator app."
        ),
    ) -> None:
        super().__init__(detail=detail, code=self.code)


class MfaEnrollmentRequiredError(AppError):
    """Raised when accessing privileged student data without enrolled MFA (HTTP 403)."""

    status_code = 403
    code = "AUTH_MFA_ENROLLMENT_REQUIRED"

    def __init__(
        self,
        detail: str = (
            "Multi-factor authentication enrollment is required to access student data."
        ),
    ) -> None:
        super().__init__(detail=detail, code=self.code)


class ResourceNotFoundError(AppError):
    """Raised when a requested resource is not found or not owned (HTTP 404).

    Does not confirm existence of unassigned course/section to non-owners (AC-002.7).
    """

    status_code = 404
    code = "RESOURCE_NOT_FOUND"

    def __init__(self, detail: str = "Resource not found.") -> None:
        super().__init__(detail=detail, code=self.code)


class AuditImmutableError(AppError):
    """Raised when attempting to modify or delete audit log entries (HTTP 403)."""

    status_code = 403
    code = "PERMISSION_DENIED"

    def __init__(
        self,
        detail: str = (
            "Audit records are immutable and append-only. "
            "Deletion is prohibited by BR-AUTH-006."
        ),
    ) -> None:
        super().__init__(detail=detail, code=self.code)


class DatabaseServiceUnavailableError(AppError):
    """Raised when database node is unreachable during auth (HTTP 503)."""

    status_code = 503
    code = "SERVICE_UNAVAILABLE"

    def __init__(
        self, detail: str = "Database service temporarily unavailable."
    ) -> None:
        super().__init__(detail=detail, code=self.code)
