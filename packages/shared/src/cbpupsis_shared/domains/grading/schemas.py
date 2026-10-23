"""Pydantic schemas (DTOs) for the grading domain."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ScholasticStandingRead(BaseModel):
    """Scholastic standing evaluation and load impact."""

    model_config = ConfigDict(from_attributes=True)

    value: str
    unit_reduction: int = 0
    explanation: str | None = None
    appeal_available: bool = False


class StudentTermGPARead(BaseModel):
    """Term GPA and calculated scholastic standing."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    student_id: uuid.UUID
    academic_term_id: uuid.UUID
    total_gpa_units: Decimal
    term_gpa: Decimal
    cumulative_gpa: Decimal
    academic_standing: str
    calculated_at: datetime
