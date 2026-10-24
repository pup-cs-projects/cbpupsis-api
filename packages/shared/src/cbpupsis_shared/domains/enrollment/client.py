"""Public enrollment interface."""

from cbpupsis_shared.domains.enrollment.service import (
    enrolled_sections,
    record_enrollments,
)

__all__ = ["enrolled_sections", "record_enrollments"]
