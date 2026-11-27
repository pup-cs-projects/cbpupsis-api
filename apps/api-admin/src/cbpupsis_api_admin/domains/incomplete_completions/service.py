"""Admin Incomplete completion review delegates to the shared grading boundary."""

from __future__ import annotations

from cbpupsis_shared.domains.grading.client import (
    decide_incomplete_completion,
    list_admin_incomplete_completions,
)

__all__ = [
    "decide_incomplete_completion",
    "list_admin_incomplete_completions",
]
