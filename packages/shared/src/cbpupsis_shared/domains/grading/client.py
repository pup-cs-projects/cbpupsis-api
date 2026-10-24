"""Academic record read interface used by the student HTTP facade."""

from cbpupsis_shared.domains.grading.service import read_grades, read_transcript

__all__ = ["read_grades", "read_transcript"]
