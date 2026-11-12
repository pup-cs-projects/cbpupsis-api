"""Exceptions for the reports domain."""

from __future__ import annotations

from cbpupsis_core.exceptions import AppError


class ReportsError(AppError):
    """Base for every error raised by the reports domain."""


class InvalidInputError(ReportsError):
    """A parameter is out of range or malformed (HTTP 400)."""

    status_code = 400
    code = "INVALID_INPUT"

    def __init__(
        self, detail: str = "One or more query parameters contain invalid values."
    ) -> None:
        super().__init__(detail)


class ResourceNotFoundError(ReportsError):
    """The requested report resource does not exist (HTTP 404)."""

    status_code = 404
    code = "RESOURCE_NOT_FOUND"

    def __init__(
        self,
        detail: str = (
            "The requested resource does not exist or is no longer available."
        ),
    ) -> None:
        super().__init__(detail)


class InsufficientRoleError(ReportsError):
    """The session role is not authorized for enrollment reports (HTTP 403)."""

    status_code = 403
    code = "AUTH_INSUFFICIENT_ROLE"

    def __init__(
        self, detail: str = "This action is not available to your role"
    ) -> None:
        super().__init__(detail)
