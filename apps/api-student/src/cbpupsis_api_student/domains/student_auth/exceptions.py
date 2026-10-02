"""Exceptions for the student auth domain."""

from __future__ import annotations

from cbpupsis_core.exceptions import AppError


class StudentAuthFailedError(AppError):
    """Uniform failure for wrong student number, birthdate, or password.

    Must never name which field failed, preserving byte-identical response.
    """

    status_code = 401
    code = "AUTH_FAILED"

    def __init__(
        self,
        detail: str = (
            "Invalid credentials. Please verify your Student ID, birthdate, "
            "and password."
        ),
    ) -> None:
        super().__init__(detail=detail, code=self.code)


class StudentAccountLockedError(AppError):
    """Account locked after consecutive failed login attempts (HTTP 423)."""

    status_code = 423
    code = "ACCOUNT_LOCKED"

    def __init__(
        self,
        retry_after_seconds: int = 900,
        detail: str = (
            "Account locked due to 5 consecutive failed attempts. "
            "Try again in 15 minutes."
        ),
    ) -> None:
        self.retry_after_seconds = retry_after_seconds
        self.data = {"retry_after_seconds": retry_after_seconds}
        super().__init__(detail=detail, code=self.code)


class StudentSessionExpiredError(AppError):
    """Session has reached 15 minutes of continuous inactivity (HTTP 401)."""

    status_code = 401
    code = "AUTH_SESSION_EXPIRED"

    def __init__(
        self, detail: str = "Your session has expired due to inactivity."
    ) -> None:
        super().__init__(detail=detail, code=self.code)


class StudentResourceNotFoundError(AppError):
    """Resource not found or horizontal access prohibited (HTTP 404)."""

    status_code = 404
    code = "RESOURCE_NOT_FOUND"

    def __init__(self, detail: str = "Resource not found.") -> None:
        super().__init__(detail=detail, code=self.code)
