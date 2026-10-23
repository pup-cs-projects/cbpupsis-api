"""Faculty facade; shared section rules preserve existing ownership checks."""

from cbpupsis_shared.domains.sections.client import (
    get_section_roster,
    list_own_sections,
)

__all__ = ["get_section_roster", "list_own_sections"]
