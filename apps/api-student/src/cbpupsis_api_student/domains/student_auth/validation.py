"""Validation rules for student authentication under BR-AUTH-001."""

from __future__ import annotations

from cbpupsis_api_student.domains.student_auth.constants import STUDENT_ID_REGEX
from cbpupsis_api_student.domains.student_auth.exceptions import (
    StudentIdFormatInvalidError,
)


def validate_student_id(student_number: str) -> None:
    """Validate the ID pattern before checking credentials.

    Must raise StudentIdFormatInvalidError (422) with zero password hash
    operations.
    Format: YYYY-XXXXX-CC-S (4-digit year, 5-digit sequence, 2-letter campus
    code, 1-digit status).
    """
    if not STUDENT_ID_REGEX.match(student_number):
        raise StudentIdFormatInvalidError()
