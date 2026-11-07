"""Pydantic schemas for the enrollment reports domain."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class EnrollmentReportRow(BaseModel):
    """An aggregated group count in the enrollment statistics report."""

    model_config = ConfigDict(from_attributes=True)

    group: str
    count: int


class EnrollmentReportRead(BaseModel):
    """Enrollment report counts by group and total."""

    model_config = ConfigDict(from_attributes=True)

    rows: list[EnrollmentReportRow]
    total: int
