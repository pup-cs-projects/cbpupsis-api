"""Schemas for the enrollment domain."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, computed_field


class TermEnrollmentRead(BaseModel):
    id: uuid.UUID
    student_id: uuid.UUID
    academic_term_id: uuid.UUID
    enrollment_status: str
    cor_downloaded: bool
    enrolled_at: datetime

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class EnrollmentSummaryRead(BaseModel):
    enrolled_units: Decimal = Field(
        ...,
        validation_alias=AliasChoices("enrolledUnits", "enrolled_units"),
    )
    effective_ceiling: Decimal = Field(
        ...,
        validation_alias=AliasChoices("effectiveCeiling", "effective_ceiling"),
    )
    prescribed_load: Decimal = Field(
        ...,
        validation_alias=AliasChoices("prescribedLoad", "prescribed_load"),
    )
    approved_overload_units: Decimal = Field(
        Decimal("0"),
        validation_alias=AliasChoices(
            "approvedOverloadUnits", "approved_overload_units"
        ),
    )
    standing_reduction: Decimal = Field(
        Decimal("0"),
        validation_alias=AliasChoices("standingReduction", "standing_reduction"),
    )
    enrolled_course_count: int = Field(
        0,
        validation_alias=AliasChoices("enrolledCourseCount", "enrolled_course_count"),
    )
    schedule_url: str = Field(
        "/api/v1/student/schedule",
        validation_alias=AliasChoices("scheduleUrl", "schedule_url"),
    )

    @computed_field
    @property
    def enrolledUnits(self) -> Decimal:
        return self.enrolled_units

    @computed_field
    @property
    def effectiveCeiling(self) -> Decimal:
        return self.effective_ceiling

    @computed_field
    @property
    def unit_ceiling(self) -> Decimal:
        return self.effective_ceiling

    @computed_field
    @property
    def prescribedLoad(self) -> Decimal:
        return self.prescribed_load

    @computed_field
    @property
    def approvedOverloadUnits(self) -> Decimal:
        return self.approved_overload_units

    @computed_field
    @property
    def standingReduction(self) -> Decimal:
        return self.standing_reduction

    @computed_field
    @property
    def enrolledCourseCount(self) -> int:
        return self.enrolled_course_count

    @computed_field
    @property
    def scheduleUrl(self) -> str:
        return self.schedule_url

    model_config = ConfigDict(populate_by_name=True)
