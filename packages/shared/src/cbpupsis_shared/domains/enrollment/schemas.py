from __future__ import annotations

import uuid
from datetime import datetime, time
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class CeilingStudent(BaseModel):
    prescribed_load: Decimal
    approved_overload_units: Decimal = Decimal("0")
    standing_reduction: Decimal = Decimal("0")
    admission_status: str = "REGULAR"


class CeilingTerm(BaseModel):
    term_type: Literal["REGULAR", "SUMMER"]


class CeilingSection(BaseModel):
    units: Decimal
    is_back_subject: bool = False


class CeilingCheckRead(BaseModel):
    attempted_units: Decimal
    effective_ceiling: Decimal


class EligibilityHoldRead(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    office: str
    type: str
    amount: Decimal
    is_blocking: bool = Field(serialization_alias="isBlocking")


class EnrollmentPeriodRead(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    start: datetime
    end: datetime
    is_open: bool = Field(serialization_alias="isOpen")


class EnrollmentEligibilityReason(BaseModel):
    code: str
    message: str


class EnrollmentEligibilityRead(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    is_eligible: bool = Field(serialization_alias="isEligible")
    term_type: Literal["REGULAR", "SUMMER"] = Field(serialization_alias="termType")
    prescribed_load: Decimal = Field(serialization_alias="prescribedLoad")
    approved_overload_units: Decimal = Field(
        serialization_alias="approvedOverloadUnits"
    )
    standing_reduction: Decimal = Field(serialization_alias="standingReduction")
    effective_ceiling: Decimal = Field(serialization_alias="effectiveCeiling")
    academic_standing: str = Field(serialization_alias="academicStanding")
    overload_eligible: bool = Field(serialization_alias="overloadEligible")
    summer_subject_cap: int | None = Field(serialization_alias="summerSubjectCap")
    holds: list[EligibilityHoldRead] = Field(default_factory=list)
    academic_period: EnrollmentPeriodRead = Field(serialization_alias="academicPeriod")
    reasons: list[EnrollmentEligibilityReason] = Field(default_factory=list)


class AvailableCourseScheduleRead(BaseModel):
    """One weekly meeting for an available section."""

    day: int = Field(ge=0, le=6, description="Meeting day; Monday=0 and Sunday=6.")
    start: time = Field(description="Meeting start time in Asia/Manila.")
    end: time = Field(description="Meeting end time in Asia/Manila.")
    room: str | None = Field(description="Assigned room, or null when unassigned.")


class AvailableCourseRead(BaseModel):
    """One curriculum and term section the signed-in student may consider."""

    model_config = ConfigDict(populate_by_name=True)

    section_id: uuid.UUID = Field(serialization_alias="sectionId")
    course_code: str = Field(serialization_alias="courseCode")
    title: str
    units: Decimal
    schedule: list[AvailableCourseScheduleRead] = Field(default_factory=list)
    faculty: str | None
    capacity: int = Field(ge=0)
    available_slots: int = Field(
        ge=0,
        serialization_alias="availableSlots",
        description=(
            "Advisory seat availability at read time; it can be stale by submit."
        ),
    )
    prerequisites_satisfied: bool = Field(
        default=True, serialization_alias="prerequisitesSatisfied"
    )
    block_reason: str | None = Field(default=None, serialization_alias="blockReason")
    conflicts_with: list[uuid.UUID] = Field(
        default_factory=list, serialization_alias="conflictsWith"
    )


class ConflictStudent(BaseModel):
    student_id: uuid.UUID


class ConflictTerm(BaseModel):
    academic_term_id: uuid.UUID


class ConflictMeeting(BaseModel):
    day: int = Field(ge=0, le=6)
    start: time
    end: time


class ConflictSection(BaseModel):
    section_id: uuid.UUID
    course_code: str
    academic_term_id: uuid.UUID
    schedule: list[ConflictMeeting] = Field(default_factory=list)


class ScheduleConflictCheckRead(BaseModel):
    conflict_free: bool = True


class EnrollmentSubmitSectionRead(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    section_id: uuid.UUID = Field(serialization_alias="sectionId")
    course_code: str = Field(serialization_alias="courseCode")
    title: str
    units: Decimal


class EnrollmentSubmitRead(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    enrollment_id: uuid.UUID = Field(serialization_alias="enrollmentId")
    status: Literal["ENROLLED_PENDING"]
    total_units: Decimal = Field(serialization_alias="totalUnits")
    sections: list[EnrollmentSubmitSectionRead]
