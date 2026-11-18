"""Faculty grade operations delegate to the shared grading boundary."""

from cbpupsis_shared.domains.grading.client import (
    file_correction,
    file_incomplete_completion,
    finalize_section_grades,
    get_section_grade_deadline,
    get_section_grades,
    list_corrections,
    list_faculty_incompletes,
    list_section_incompletes,
    read_correction,
    save_section_grade_draft,
)

__all__ = [
    "file_correction",
    "file_incomplete_completion",
    "finalize_section_grades",
    "get_section_grade_deadline",
    "get_section_grades",
    "list_corrections",
    "list_faculty_incompletes",
    "list_section_incompletes",
    "read_correction",
    "save_section_grade_draft",
]
