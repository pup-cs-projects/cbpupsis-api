"""Enrollment DTOs, without another student's data."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pydantic.alias_generators import to_camel


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


class AcademicDTO(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        extra="forbid",
    )


class AcademicBlock(AcademicDTO):
    rule: str
    course_code: str
    blocking_mark: str | None = None
    required_range: str | None = None
    details: dict[str, object] = Field(default_factory=dict)


class AcademicSectionVerdict(AcademicDTO):
    prerequisites_satisfied: bool = True
    block_reason: str | None = None
    blocks: list[AcademicBlock] = Field(default_factory=list)
    waiver_available: bool = False


class AcademicVerdict(AcademicDTO):
    sections: dict[uuid.UUID, AcademicSectionVerdict] = Field(default_factory=dict)
    blockers: list[AcademicBlock] = Field(default_factory=list)


WaiverCondition = Literal["REPEATED_PREREQUISITE", "SUPERIOR_STANDING", "GRADUATING"]
Nonblank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class WaiverFile(AcademicDTO):
    section_id: uuid.UUID
    prerequisite_course_code: Annotated[Nonblank, Field(max_length=20)]
    condition: WaiverCondition
    justification: Annotated[Nonblank, Field(max_length=4000)]


class WaiverDecision(AcademicDTO):
    decision: Literal["APPROVE", "REJECT"]
    remarks: str | None = Field(default=None, max_length=4000)
    vpaa_reference: str | None = Field(default=None, max_length=128)


class WaiverFiled(AcademicDTO):
    waiver_request_id: uuid.UUID
    state: str


class WaiverRead(WaiverFiled):
    student_id: uuid.UUID
    section_id: uuid.UUID
    prerequisite_course_code: str
    condition: str
    justification: str
    review_remarks: str | None
    vpaa_reference: str | None
    created_at: datetime


class WaiverReviewer(BaseModel):
    id: uuid.UUID
    position: Literal["chairperson", "dean", "registrar"]
    department: str | None = None
    college: str | None = None
