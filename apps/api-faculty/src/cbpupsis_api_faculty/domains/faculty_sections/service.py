"""Faculty section operations delegate to the shared ownership boundary."""

from cbpupsis_shared.domains.sections.client import (
    get_section_roster,
    list_own_sections,
)

__all__ = ["get_section_roster", "list_own_sections"]
