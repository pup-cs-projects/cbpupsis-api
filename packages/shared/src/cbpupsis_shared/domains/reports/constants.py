"""Constants for the reports domain."""

from __future__ import annotations

READ_ENROLLMENT_REPORT = "ReadEnrollmentReport"

VALID_GROUP_BY: frozenset[str] = frozenset(
    {"program", "status", "yearLevel", "year_level", "sex", "section"}
)

VALID_EXPORT_FORMATS: frozenset[str] = frozenset({"csv", "pdf"})
