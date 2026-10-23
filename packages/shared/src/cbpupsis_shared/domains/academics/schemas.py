"""Schemas for the academics domain."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, computed_field


class AcademicTermRead(BaseModel):
    id: uuid.UUID
    academic_year: str = Field(
        ...,
        validation_alias=AliasChoices("academicYear", "academic_year"),
    )
    semester: str
    term_type: str = Field(
        "REGULAR",
        validation_alias=AliasChoices("termType", "term_type"),
    )
    is_current: bool = Field(
        False,
        validation_alias=AliasChoices("isCurrent", "is_current"),
    )
    start_date: date | None = Field(
        None,
        validation_alias=AliasChoices("startDate", "start_date"),
    )
    end_date: date | None = Field(
        None,
        validation_alias=AliasChoices("endDate", "end_date"),
    )
    early_enrollment_start: datetime | None = Field(
        None,
        validation_alias=AliasChoices("earlyEnrollmentStart", "early_enrollment_start"),
    )
    early_enrollment_end: datetime | None = Field(
        None,
        validation_alias=AliasChoices("earlyEnrollmentEnd", "early_enrollment_end"),
    )
    regular_enrollment_start: datetime | None = Field(
        None,
        validation_alias=AliasChoices(
            "regularEnrollmentStart", "regular_enrollment_start"
        ),
    )
    regular_enrollment_end: datetime | None = Field(
        None,
        validation_alias=AliasChoices("regularEnrollmentEnd", "regular_enrollment_end"),
    )
    late_enrollment_end: datetime | None = Field(
        None,
        validation_alias=AliasChoices("lateEnrollmentEnd", "late_enrollment_end"),
    )
    add_drop_deadline: datetime | None = Field(
        None,
        validation_alias=AliasChoices("addDropDeadline", "add_drop_deadline"),
    )
    grade_submission_deadline: datetime | None = Field(
        None,
        validation_alias=AliasChoices(
            "gradeSubmissionDeadline", "grade_submission_deadline"
        ),
    )

    @computed_field
    @property
    def academicYear(self) -> str:
        return self.academic_year

    @computed_field
    @property
    def termType(self) -> str:
        return self.term_type

    @computed_field
    @property
    def isCurrent(self) -> bool:
        return self.is_current

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class AcademicPeriodRead(BaseModel):
    type: str
    label: str
    opens_at: datetime = Field(
        ...,
        validation_alias=AliasChoices("opensAt", "opens_at"),
    )
    closes_at: datetime = Field(
        ...,
        validation_alias=AliasChoices("closesAt", "closes_at"),
    )
    is_open: bool = Field(
        ...,
        validation_alias=AliasChoices("isOpen", "is_open"),
    )
    days_remaining: int = Field(
        ...,
        validation_alias=AliasChoices("daysRemaining", "days_remaining"),
    )

    @computed_field
    @property
    def opensAt(self) -> datetime:
        return self.opens_at

    @computed_field
    @property
    def closesAt(self) -> datetime:
        return self.closes_at

    @computed_field
    @property
    def isOpen(self) -> bool:
        return self.is_open

    @computed_field
    @property
    def daysRemaining(self) -> int:
        return self.days_remaining

    model_config = ConfigDict(populate_by_name=True)


class AcademicEventRead(BaseModel):
    id: str
    title: str
    starts_at: datetime = Field(
        ...,
        validation_alias=AliasChoices("startsAt", "starts_at"),
    )
    ends_at: datetime = Field(
        ...,
        validation_alias=AliasChoices("endsAt", "ends_at"),
    )
    is_multi_day: bool = Field(
        False,
        validation_alias=AliasChoices("isMultiDay", "is_multi_day"),
    )
    category: str = Field("ACADEMIC_CALENDAR")
    description: str | None = None

    @computed_field
    @property
    def startsAt(self) -> datetime:
        return self.starts_at

    @computed_field
    @property
    def endsAt(self) -> datetime:
        return self.ends_at

    @computed_field
    @property
    def isMultiDay(self) -> bool:
        return self.is_multi_day

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
