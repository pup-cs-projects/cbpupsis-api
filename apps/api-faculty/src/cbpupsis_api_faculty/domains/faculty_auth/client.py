"""Internal client for the faculty authentication domain — the extraction swap point.

Other domains depend on THIS module, never on ``service.py`` directly, and it
returns DTOs rather than ORM objects. If faculty_auth is extracted into its own
service, only this file changes.
"""

from __future__ import annotations

from cbpupsis_api_faculty.domains.faculty_auth.validation import (
    validate_faculty_identifier,
)


def validate_identifier(identifier: str | None) -> bool:
    """Validate faculty identifier format without querying credentials."""
    return validate_faculty_identifier(identifier)
