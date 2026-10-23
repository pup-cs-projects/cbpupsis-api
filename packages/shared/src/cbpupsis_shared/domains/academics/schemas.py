"""Pydantic schemas (DTOs) for the academics domain."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class AcademicTermRead(BaseModel):
    """An academic term and its key enrollment and grading dates."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    academic_year: str
    semester: str
    term_type: str = "REGULAR"
    is_current: bool = False
    start_date: date | None = None
    end_date: date | None = None
    early_enrollment_start: datetime | None = None
    early_enrollment_end: datetime | None = None
    regular_enrollment_start: datetime | None = None
    regular_enrollment_end: datetime | None = None
    late_enrollment_end: datetime | None = None
    add_drop_deadline: datetime | None = None
    grade_submission_deadline: datetime | None = None


class AcademicPeriodRead(BaseModel):
    """An active or upcoming enrollment / adjustment period."""

    model_config = ConfigDict(from_attributes=True)

    type: str
    label: str
    opens_at: datetime
    closes_at: datetime
    is_open: bool
    days_remaining: int


class AcademicEventRead(BaseModel):
    """An academic calendar event."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    starts_at: datetime
    ends_at: datetime
    is_multi_day: bool = False
    category: str = "ACADEMIC_CALENDAR"
    description: str | None = None
