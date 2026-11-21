"""Administrative facade over the shared grading owner."""

from cbpupsis_shared.domains.grading.client import (
    decide_correction,
    list_corrections,
    read_correction,
)

__all__ = ["decide_correction", "list_corrections", "read_correction"]
