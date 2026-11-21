"""Exceptions for the student dashboard domain."""

from __future__ import annotations

from cbpupsis_core.exceptions import AppError


class InvalidInputError(AppError):
    """A limit parameter is out of range or a parameter is malformed (HTTP 400)."""

    status_code = 400
    code = "INVALID_INPUT"

    def __init__(
        self, detail: str = "One or more query parameters contain invalid values."
    ) -> None:
        super().__init__(detail)


class ResourceNotFoundError(AppError):
    """The requested dashboard is not the caller's own (HTTP 404).

    Answering 404 rather than 403 prevents student ID probing per BR-AUTH-005.
    """

    status_code = 404
    code = "RESOURCE_NOT_FOUND"

    DEFAULT_MESSAGE = "The requested resource does not exist or is no longer available."

    def __init__(
        self,
        detail: str = DEFAULT_MESSAGE,
    ) -> None:
        super().__init__(detail)
