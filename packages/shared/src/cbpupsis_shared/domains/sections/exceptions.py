"""Domain exceptions for course section administration."""

from __future__ import annotations

from cbpupsis_core.exceptions import AppError, ConflictError


class SectionError(AppError):
    """Base exception for course section errors."""


class SectionCapacityBelowEnrolledError(SectionError, ConflictError):
    """Section capacity cannot be reduced below current enrolled count (HTTP 409)."""

    code = "SECTION_CAPACITY_BELOW_ENROLLED"

    def __init__(self, enrolled: int) -> None:
        super().__init__(
            "Section capacity cannot be set below the currently enrolled count "
            f"({enrolled}).",
            response_fields={"details": {"enrolled": enrolled}},
        )
        self.enrolled = enrolled
