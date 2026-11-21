"""Preserve the faculty contract while shared sections own the DTOs."""

from cbpupsis_shared.domains.sections.schemas import (
    CourseSectionRead,
    CourseSectionRosterRead,
)

__all__ = ["CourseSectionRead", "CourseSectionRosterRead"]
