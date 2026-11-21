"""Errors the student_profile domain raises (US-STU-002)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from cbpupsis_api_student.domains.student_profile.constants import (
    AVATAR_MAX_BYTES,
    PETITION_ROUTE,
)
from cbpupsis_core.exceptions import AppError, ForbiddenError, ValidationError


class StudentProfileError(AppError):
    """Base for every error raised by the student_profile domain."""


class ProfileFieldLockedError(StudentProfileError, ForbiddenError):
    """An attempt was made to modify one or more locked master fields (HTTP 403).

    Names the locked fields and offers the Registrar petition route (AC-102.3).
    """

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


class ProfileValidationError(StudentProfileError, ValidationError):
    """One or more profile or emergency-contact fields failed validation (HTTP 422).

    Names every offending field and its expected shape (AC-102.4).
    """

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


class AvatarFileTooLargeError(StudentProfileError):
    """The uploaded profile picture exceeds 5 MB (HTTP 413, AC-102.7)."""

    status_code = 413
    code = "FILE_TOO_LARGE"

    def __init__(self) -> None:
        super().__init__(
            "File exceeds the 5 MB limit. Upload a JPEG or PNG image up to 5 MB.",
            response_fields={
                "maxBytes": AVATAR_MAX_BYTES,
                "acceptedFormats": ["image/jpeg", "image/png"],
            },
        )


class AvatarUnsupportedMediaTypeError(StudentProfileError):
    """The uploaded file content is not a valid JPEG or PNG (HTTP 415, AC-102.7)."""

    status_code = 415
    code = "UNSUPPORTED_MEDIA_TYPE"

    def __init__(self) -> None:
        super().__init__(
            "Unsupported file format. Accepted formats are JPEG and PNG up to 5 MB.",
            response_fields={
                "acceptedFormats": ["image/jpeg", "image/png"],
                "maxBytes": AVATAR_MAX_BYTES,
            },
        )
