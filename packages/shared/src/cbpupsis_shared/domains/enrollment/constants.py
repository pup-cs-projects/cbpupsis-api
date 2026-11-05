"""Fixed values for the enrollment domain."""

from __future__ import annotations

from decimal import Decimal

READ_ENROLLMENT = "ReadEnrollment"

MAX_OVERLOAD_UNITS = Decimal("6")
SUMMER_SUBJECT_CAP = 2
SUMMER_RESTRICTED_ADMISSION_STATUSES = frozenset(
    {"PROBATIONARY", "SHIFTEE", "TRANSFEREE"}
)
BLOCKED_ACADEMIC_STANDINGS = frozenset(
    {"DISMISSED", "PERMANENTLY DISQUALIFIED", "LEAVE_OF_ABSENCE", "LOA"}
)
