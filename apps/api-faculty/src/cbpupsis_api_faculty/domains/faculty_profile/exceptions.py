"""Exceptions raised by the faculty_profile domain (US-FAC-005)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from cbpupsis_api_faculty.domains.faculty_profile.constants import PETITION_ROUTE
from cbpupsis_core.exceptions import AppError, ForbiddenError, ValidationError


class FacultyProfileError(AppError):
    """Base for every error raised by the faculty_profile domain."""


class ProfileFieldLockedError(FacultyProfileError, ForbiddenError):
    """Attempted to modify one or more locked master fields (HTTP 403)."""

    code = "PROFILE_FIELD_LOCKED"

    def __init__(self, locked_fields: Sequence[str]) -> None:
        fields = list(dict.fromkeys(locked_fields))
        details = {
            "lockedFields": fields,
            "petitionRoute": PETITION_ROUTE,
        }
        super().__init__(
            "One or more fields cannot be changed here.",
            response_fields={
                "lockedFields": fields,
                "petitionRoute": PETITION_ROUTE,
                "details": details,
            },
        )


class ProfileValidationError(FacultyProfileError, ValidationError):
    """One or more profile fields failed validation (HTTP 422)."""

    code = "VALIDATION_ERROR"

    def __init__(self, field_errors: Mapping[str, str]) -> None:
        errors = dict(field_errors)
        fields = list(errors)
        if len(errors) == 1:
            name, reason = next(iter(errors.items()))
            message = f"Validation failed for {name}: {reason}"
        else:
            summary = "; ".join(f"{name}: {reason}" for name, reason in errors.items())
            message = f"Validation failed for {', '.join(fields)}: {summary}"
        super().__init__(
            message,
            response_fields={
                "fields": fields,
                "errors": errors,
                "details": {"fields": fields, "errors": errors},
            },
        )
