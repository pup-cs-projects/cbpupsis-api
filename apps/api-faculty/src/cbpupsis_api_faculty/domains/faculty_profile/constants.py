"""Constants and field mappings for the faculty_profile domain (US-FAC-005)."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from cbpupsis_shared.domains.users.constants import (
    ACADEMIC_RANK_MAX_LENGTH,
    OFFICE_HOURS_MAX_LENGTH,
    OFFICE_LOCATION_MAX_LENGTH,
    QUALIFICATIONS_MAX_LENGTH,
)

PETITION_ROUTE = "/api/v1/faculty/petitions/record-correction"
MANILA_TZ = ZoneInfo("Asia/Manila")

LOCKED_FIELDS: tuple[str, ...] = (
    "fullName",
    "employeeId",
    "department",
)
LOCKED_FIELD_SET: frozenset[str] = frozenset(LOCKED_FIELDS)

#: Editable camelCase API fields mapped to FacultyProfile column names.
EDITABLE_FIELD_COLUMNS: dict[str, str] = {
    "title": "academic_rank",
    "officeLocation": "office_location",
    "officeHours": "office_hours",
    "qualifications": "qualifications",
}

EDITABLE_FIELD_LIMITS: dict[str, int] = {
    "title": ACADEMIC_RANK_MAX_LENGTH,
    "officeLocation": OFFICE_LOCATION_MAX_LENGTH,
    "officeHours": OFFICE_HOURS_MAX_LENGTH,
    "qualifications": QUALIFICATIONS_MAX_LENGTH,
}
