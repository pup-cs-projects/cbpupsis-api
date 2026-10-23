"""Schemas for the grading domain."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, computed_field


class ScholasticStandingRead(BaseModel):
    value: str
    unit_reduction: int = Field(
        0,
        validation_alias=AliasChoices("unitReduction", "unit_reduction"),
    )
    explanation: str | None = None
    appeal_available: bool = Field(
        False,
        validation_alias=AliasChoices("appealAvailable", "appeal_available"),
    )

    @computed_field
    @property
    def unitReduction(self) -> int:
        return self.unit_reduction

    @computed_field
    @property
    def appealAvailable(self) -> bool:
        return self.appeal_available

    model_config = ConfigDict(populate_by_name=True)


class StudentTermGPARead(BaseModel):
    id: uuid.UUID
    student_id: uuid.UUID
    academic_term_id: uuid.UUID
    total_gpa_units: Decimal
    term_gpa: Decimal
    cumulative_gpa: Decimal
    academic_standing: str
    calculated_at: datetime

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
