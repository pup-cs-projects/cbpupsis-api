"""Correction of Entry contracts shared by faculty and administrative facades."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

CorrectionState = Literal[
    "PENDING_CHAIR", "PENDING_DEAN", "PENDING_REGISTRAR", "COMMITTED", "REJECTED"
]


class CorrectionDTO(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )


class CorrectionDecision(CorrectionDTO):
    action: Literal["ENDORSE", "COMMIT", "REJECT"]
    version: int = Field(ge=1)
    remarks: str | None = Field(default=None, max_length=4000)

    @field_validator("remarks")
    @classmethod
    def trim_remarks(cls, value: str | None) -> str | None:
        return value.strip() if value else None


class CorrectionRead(CorrectionDTO):
    id: uuid.UUID
    grade_id: uuid.UUID
    student_id: uuid.UUID
    section_id: uuid.UUID
    faculty_id: uuid.UUID
    current_mark: str
    proposed_mark: str
    reason: str
    grading_sheet_file_id: str
    grading_sheet_content_type: str | None
    grading_sheet_size: int | None
    grading_sheet_sha256: str | None
    state: CorrectionState
    version: int
    window_closed_at: datetime | None
    chair_approved_by: uuid.UUID | None
    chair_approved_at: datetime | None
    dean_approved_by: uuid.UUID | None
    dean_approved_at: datetime | None
    registrar_approved_by: uuid.UUID | None
    registrar_approved_at: datetime | None
    rejected_by: uuid.UUID | None
    rejection_remarks: str | None
    resolved_at: datetime | None
    created_at: datetime


class CorrectionPage(CorrectionDTO):
    items: list[CorrectionRead]
    total: int
    limit: int
    offset: int
