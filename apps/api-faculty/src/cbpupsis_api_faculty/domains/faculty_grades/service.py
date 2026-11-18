"""Faculty grade operations delegate to the shared grading boundary."""

from cbpupsis_shared.domains.grading.client import (
    finalize_section_grades,
    get_section_grade_deadline,
    get_section_grades,
    save_section_grade_draft,
)

__all__ = [
    "finalize_section_grades",
    "get_section_grade_deadline",
    "get_section_grades",
    "save_section_grade_draft",
]
