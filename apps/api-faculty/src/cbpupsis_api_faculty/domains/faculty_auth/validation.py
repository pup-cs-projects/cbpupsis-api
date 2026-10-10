"""Validation rules for faculty authentication."""

from __future__ import annotations

import re

# Format: YYYY-NNNNN-XX-N (e.g. 2021-00456-MN-0)
FACULTY_ID_REGEX = re.compile(r"^(\d{4})-(\d{5})-([A-Z]{2})-(\d)$")


def validate_faculty_identifier(identifier: str | None) -> bool:
    """Validate that the identifier matches the institutional format YYYY-NNNNN-XX-N."""
    if not identifier or not isinstance(identifier, str):
        return False
    return bool(FACULTY_ID_REGEX.match(identifier.strip()))
