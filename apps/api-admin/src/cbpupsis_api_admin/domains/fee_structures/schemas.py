"""Admin fee structure schemas."""

from __future__ import annotations

from cbpupsis_shared.domains.billing.schemas import (
    FeeStructureRead,
    FeeStructureSaveRequest,
    InstallmentItem,
    InstallmentItemRead,
    ProgramTuitionRate,
    ProgramTuitionRateRead,
)

__all__ = [
    "FeeStructureRead",
    "FeeStructureSaveRequest",
    "InstallmentItem",
    "InstallmentItemRead",
    "ProgramTuitionRate",
    "ProgramTuitionRateRead",
]
