from __future__ import annotations

import uuid
from datetime import datetime, time
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


class CurrentEnrollmentRead(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    enrollment_id: uuid.UUID = Field(
        serialization_alias="enrollmentId",
        description="Same id as enrollmentId in the submit 201 response.",
    )
    status: str = Field(
        description=(
            "Stored term enrollment status: ENROLLED_PENDING after submit, "
            "OFFICIALLY_ENROLLED once the certificate is issued."
        ),
    )
    total_units: Decimal = Field(
        serialization_alias="totalUnits",
        description="Units of the sections currently held (dropped ones excluded).",
    )


class EnrollmentEligibilityRead(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    is_eligible: bool = Field(serialization_alias="isEligible")
    term: str = Field(
        description=(
            "Display label of the current term, for example 2026-2027-First "
            "Semester. Display only: endpoints take academicTermId."
        )
    )
    academic_term_id: uuid.UUID = Field(
        serialization_alias="academicTermId",
        description=(
            "Current academic-term UUID. Send it as academicTermId to every "
            "student enrollment, certificate and assessment endpoint."
        ),
    )
    term_type: Literal["REGULAR", "SUMMER"] = Field(serialization_alias="termType")
    curriculum_id: uuid.UUID | None = Field(
        default=None, serialization_alias="curriculumId"
    )
    curriculum_version: str | None = Field(
        default=None, serialization_alias="curriculumVersion"
    )
    prescribed_load: Decimal = Field(serialization_alias="prescribedLoad")
    approved_overload_units: Decimal = Field(
        serialization_alias="approvedOverloadUnits"
    )
    pending_overload_units: Decimal = Field(
        default=Decimal("0"),
        serialization_alias="pendingOverloadUnits",
        description=(
            "Units in overload requests still waiting for the Dean this term. "
            "Not counted in effectiveCeiling until approved."
        ),
    )
    standing_reduction: Decimal = Field(serialization_alias="standingReduction")
    effective_ceiling: Decimal = Field(serialization_alias="effectiveCeiling")
    academic_standing: str = Field(serialization_alias="academicStanding")
    overload_eligible: bool = Field(serialization_alias="overloadEligible")
    summer_subject_cap: int | None = Field(serialization_alias="summerSubjectCap")
    holds: list[EligibilityHoldRead] = Field(default_factory=list)
    academic_period: EnrollmentPeriodRead = Field(serialization_alias="academicPeriod")
    current_enrollment: CurrentEnrollmentRead | None = Field(
        default=None,
        serialization_alias="currentEnrollment",
        description=(
            "The student's enrollment for this term, or null when they have not "
            "submitted yet. When set, show the enrolled state, not the picker."
        ),
    )
    add_drop_period: EnrollmentPeriodRead | None = Field(
        default=None,
        serialization_alias="addDropPeriod",
        description=(
            "Window in which add and drop requests are accepted, or null when the "
            "term has no add and drop dates configured."
        ),
    )
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
    blocks: list[AcademicBlock] = Field(default_factory=list)
    waiver_available: bool = Field(default=False, serialization_alias="waiverAvailable")
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


class OverloadEligibilityRead(BaseModel):
    eligible: bool
    is_graduating: bool
    gwa: Decimal | None
    required_gwa: Decimal = Field(serialization_alias="requiredGwa")


class OverloadRequestFiledRead(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    overload_request_id: uuid.UUID = Field(serialization_alias="overloadRequestId")
    state: Literal["PENDING_DEAN"]


class OverloadRequestRead(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    overload_request_id: uuid.UUID = Field(serialization_alias="overloadRequestId")
    student_id: uuid.UUID = Field(serialization_alias="studentId")
    student_number: str = Field(serialization_alias="studentNumber")
    student_name: str = Field(serialization_alias="studentName")
    program_code: str = Field(serialization_alias="programCode")
    department: str
    college: str | None
    term: str
    requested_units: int = Field(serialization_alias="requestedUnits")
    justification: str
    state: Literal["PENDING_DEAN", "APPROVED", "REJECTED"]
    reviewed_by: uuid.UUID | None = Field(
        default=None, serialization_alias="reviewedBy"
    )
    remarks: str | None = None
    created_at: datetime = Field(serialization_alias="createdAt")
    reviewed_at: datetime | None = Field(default=None, serialization_alias="reviewedAt")


class AddSectionItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    section_id: uuid.UUID = Field(alias="sectionId")


class DropSectionItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    section_id: uuid.UUID = Field(alias="sectionId")
    reason: str


class AddDropRequestCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    add: list[AddSectionItem] = Field(default_factory=list)
    drop: list[DropSectionItem] = Field(default_factory=list)
    comments: str | None = None


class AddDropCourseRead(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    section_id: uuid.UUID = Field(alias="sectionId")
    course_code: str = Field(alias="courseCode")
    course_title: str = Field(alias="courseTitle")
    units: Decimal
    tag: Literal["PENDING_ADD", "PENDING_DROP"]
    reason: str | None = None


class AddDropRequestRead(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    request_id: uuid.UUID = Field(alias="requestId")
    student_id: uuid.UUID = Field(alias="studentId")
    academic_term_id: uuid.UUID = Field(alias="academicTermId")
    state: Literal["PENDING", "APPROVED", "REJECTED"]
    units_before: Decimal = Field(alias="unitsBefore")
    units_after: Decimal = Field(alias="unitsAfter")
    comments: str | None = None
    remarks: str | None = None
    version: int = 1
    courses: list[AddDropCourseRead] = Field(default_factory=list)
    created_at: datetime = Field(alias="createdAt")
    reviewed_at: datetime | None = Field(default=None, alias="reviewedAt")
    reviewed_by: uuid.UUID | None = Field(default=None, alias="reviewedBy")


class AddDropDecisionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    decision: Literal["APPROVE", "REJECT"]
    remarks: str | None = None
    version: int


class AdminAddDropRequestRead(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    request_id: uuid.UUID = Field(alias="requestId")
    student_id: uuid.UUID = Field(alias="studentId")
    student_number: str = Field(alias="studentNumber")
    student_name: str = Field(alias="studentName")
    program_code: str = Field(alias="programCode")
    department: str
    college: str | None
    term: str
    academic_term_id: uuid.UUID = Field(alias="academicTermId")
    state: Literal["PENDING", "APPROVED", "REJECTED"]
    units_before: Decimal = Field(alias="unitsBefore")
    units_after: Decimal = Field(alias="unitsAfter")
    comments: str | None = None
    remarks: str | None = None
    version: int = 1
    courses: list[AddDropCourseRead] = Field(default_factory=list)
    created_at: datetime = Field(alias="createdAt")
    reviewed_at: datetime | None = Field(default=None, alias="reviewedAt")
    reviewed_by: uuid.UUID | None = Field(default=None, alias="reviewedBy")
