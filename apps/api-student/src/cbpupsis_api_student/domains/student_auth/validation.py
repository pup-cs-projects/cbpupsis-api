"""Validation rules for student authentication under BR-AUTH-001."""

from __future__ import annotations

import re

from cbpupsis_core.exceptions import AppError

# Format: YYYY-NNNNN-XX-N (e.g. 2021-00123-MN-0)
STUDENT_ID_REGEX = re.compile(r"^(\d{4})-(\d{5})-([A-Z]{2})-(\d)$")


class StudentIdFormatInvalidError(AppError):
    """Raised when student number format or check digit is invalid (HTTP 422)."""

    status_code = 422
    code = "AUTH_ID_FORMAT_INVALID"

    def __init__(self, detail: str = "Invalid Student ID format.") -> None:
        super().__init__(detail=detail, code=self.code)


def calculate_check_digit(year: str, sequence: str) -> int:
    """Calculate the BR-AUTH-001 check digit for the 9 numeric digits."""
    digits = [int(c) for c in year + sequence]
    weights = [1, 1, 1, 0, 1, 1, 1, 1, 1]
    return sum(d * w for d, w in zip(digits, weights, strict=False)) % 10


def validate_student_id(student_number: str) -> None:
    """Validate the ID pattern and check digit before checking credentials.

    Must raise StudentIdFormatInvalidError (422) with zero password hash operations.
    """
    match = STUDENT_ID_REGEX.match(student_number)
    if not match:
        raise StudentIdFormatInvalidError()

    year, sequence, _campus, check_str = match.groups()
    expected_check_digit = calculate_check_digit(year, sequence)
    if int(check_str) != expected_check_digit:
        raise StudentIdFormatInvalidError()
