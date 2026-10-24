"""Enrollment DTOs, without another student's data."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, model_validator


class TermEnrollmentRead(BaseModel):
    """A student's enrollment record for an academic term."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    student_id: uuid.UUID
    academic_term_id: uuid.UUID
    enrollment_status: str
    cor_downloaded: bool
    enrolled_at: datetime


class EnrollmentSummaryRead(BaseModel):
    """Enrolled load, limits, and schedule link for a term."""

    model_config = ConfigDict(from_attributes=True)

    enrolled_units: Decimal
    effective_ceiling: Decimal
    unit_ceiling: Decimal | None = None
    prescribed_load: Decimal
    approved_overload_units: Decimal = Decimal("0")
    standing_reduction: Decimal = Decimal("0")
    enrolled_course_count: int = 0
    schedule_url: str = "/api/v1/student/schedule"

    @model_validator(mode="after")
    def populate_unit_ceiling(self) -> EnrollmentSummaryRead:
        if self.unit_ceiling is None:
            self.unit_ceiling = self.effective_ceiling
        return self


class RecordEnrollment(BaseModel):
    id: uuid.UUID
    academic_term_id: uuid.UUID
    section_id: uuid.UUID


class EnrolledSection(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    section_id: uuid.UUID
    status: str
