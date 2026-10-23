"""Section data access through DTOs, never cross-app imports."""

from cbpupsis_shared.domains.sections.service import (
    get_section_roster,
    list_own_sections,
    record_sections,
)

__all__ = ["get_section_roster", "list_own_sections", "record_sections"]
