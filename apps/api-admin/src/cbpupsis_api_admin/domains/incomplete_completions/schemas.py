"""Preserve the admin contract while shared grading owns the DTOs."""

from __future__ import annotations

from cbpupsis_shared.domains.grading.schemas import (
    IncompleteCompletionDecisionRequest,
    IncompleteCompletionRead,
)

__all__ = [
    "IncompleteCompletionDecisionRequest",
    "IncompleteCompletionRead",
]
