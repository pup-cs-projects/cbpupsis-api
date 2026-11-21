"""Faculty section operations delegate to the shared ownership boundary."""

from cbpupsis_shared.domains.sections.client import (
    export_section_roster_csv,
    get_section_roster,
    get_section_student,
    get_section_student_record,
    list_own_sections,
)

__all__ = [
    "export_section_roster_csv",
    "get_section_roster",
    "get_section_student",
    "get_section_student_record",
    "list_own_sections",
]
