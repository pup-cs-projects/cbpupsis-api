"""Student record contracts; Decimal values serialize as strings, not floats."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from cbpupsis_shared.domains.academics.schemas import TermRead
from cbpupsis_shared.domains.grading.constants import (
    NUMERIC_ALLOWED_MARKS,
    AcademicStanding,
)
from cbpupsis_shared.domains.grading.correction_schemas import (
    CorrectionDecision as CorrectionDecision,
)
from cbpupsis_shared.domains.grading.correction_schemas import (
    CorrectionPage as CorrectionPage,
)
from cbpupsis_shared.domains.grading.correction_schemas import (
    CorrectionRead as CorrectionRead,
)
from cbpupsis_shared.domains.users.schemas import RecordIdentity


class ScholasticStandingRead(BaseModel):
    """Scholastic standing evaluation and load impact."""

    model_config = ConfigDict(from_attributes=True)

    value: str | None = None
    unit_reduction: int | None = 0
    explanation: str | None = None
    appeal_available: bool = False
    state: Literal["available", "error"] = "available"
    error: str | None = None


class StudentTermGPARead(BaseModel):
    """Term GPA and calculated scholastic standing."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    student_id: uuid.UUID
    academic_term_id: uuid.UUID
    total_gpa_units: Decimal
    term_gpa: Decimal | None
    cumulative_gpa: Decimal | None
    academic_standing: str
    calculated_at: datetime


FinalGrade = Literal[
    "1.00",
    "1.25",
    "1.50",
    "1.75",
    "2.00",
    "2.25",
    "2.50",
    "2.75",
    "3.00",
    "5.00",
    "PASS",
    "FAIL",
    "Inc",
    "W",
]


class AcademicAttempt(BaseModel):
    enrollment_item_id: uuid.UUID
    course_id: uuid.UUID
    mark: str


class SectionRosterGrade(BaseModel):
    enrollment_id: uuid.UUID
    grade_status: Literal["PENDING", "DRAFT", "SUBMITTED", "INC"]


class AcademicRecord(BaseModel):
    attempts: list[AcademicAttempt]
    cumulative_gwa: Decimal | None


class GradeCourse(BaseModel):
    """One posted course attempt; draft marks are excluded."""

    course_id: uuid.UUID = Field(
        description="Catalog course ID used to deduplicate credits."
    )
    enrollment_id: uuid.UUID = Field(
        description="Course-enrollment ID for this attempt."
    )
    code: str = Field(description="Catalog course code.", examples=["COMP101"])
    title: str = Field(description="Catalog course title.")
    units: Decimal = Field(
        description="Lecture plus laboratory units, serialized as a Decimal string.",
        examples=["3.0"],
    )
    final_grade: FinalGrade = Field(
        description="Published final mark. Numeric marks have two places; "
        "PASS/FAIL apply to Pass/Fail sections; Inc is incomplete, W is withdrawn.",
        examples=["1.25", "PASS", "FAIL", "Inc", "W"],
    )
    status: Literal["Passed", "Failed", "Incomplete", "Withdrawn"] = Field(
        description="Readable outcome for this attempt; Inc is not a numeric failure."
    )
    posted_at: datetime = Field(description="Stored final-submission timestamp in UTC.")
    completion_deadline: datetime | None = Field(
        default=None,
        description="Stored UTC completion deadline for Inc; null for other marks. "
        "An expired deadline does not automatically change the mark on read.",
    )


class GradesRead(BaseModel):
    """One student's selected term, published marks and stored standing.

    Decimal values are JSON strings. Averages use ROUND_HALF_UP to two
    places. Empty or wholly nonnumeric/NSTP results have a null GWA,
    never a fabricated zero. Standing is not inferred from these marks.
    """

    student: RecordIdentity
    term: TermRead | None = Field(
        default=None,
        description="Selected term, or null when no matching term is found.",
    )
    courses: list[GradeCourse] = Field(
        default_factory=list, description="Posted attempts, sorted by course code."
    )
    gwa: Decimal | None = Field(
        default=None,
        description="Unit-weighted term average as a two-place Decimal string. "
        "Excludes NSTP and nonnumeric marks, includes numeric failures; null "
        "when there are no eligible numeric units.",
        examples=["1.63"],
    )
    units_earned: Decimal = Field(
        default=Decimal("0.0"),
        description="Units of unique passed courses in this term, as a Decimal string. "
        "Failures, withdrawals and unresolved incompletes earn no units.",
    )
    academic_standing: AcademicStanding | None = Field(
        default=None,
        description="Stored selected-term evaluation, not recalculated from GWA. "
        "Null when not evaluated; Suspension is not an allowed standing.",
    )
    unit_reduction: int | None = Field(
        default=None,
        serialization_alias="unitReduction",
        description="Standing-based unit reduction: 0 for Good Standing/Warning "
        "Level 1, 3 for Warning Level 2, 6 for Probation. Null when unevaluated "
        "or when dropping/dismissal requires the separate enrollment workflow.",
    )
    standing_state: Literal["evaluated", "not_evaluated"] = Field(
        default="not_evaluated", description="Whether a stored term evaluation exists."
    )
    standing_message: str | None = Field(
        default="Academic standing has not yet been evaluated.",
        description="Explanation for unevaluated standing; null when evaluated.",
    )
    standing_evaluated_at: datetime | None = Field(
        default=None, description="Stored evaluation timestamp in UTC, or null."
    )
    state: Literal["available", "empty"] = Field(
        default="empty", description="Whether the selection contains published marks."
    )
    message: str | None = Field(
        default="No finalized marks are available for this selection.",
        description="Empty-selection explanation; null when marks are available.",
    )


class TranscriptTerm(BaseModel):
    """One term's posted attempts and running totals through that term."""

    term: TermRead
    courses: list[GradeCourse]
    gwa: Decimal | None = Field(description="Term GWA using the same rules as grades.")
    units_earned: Decimal = Field(
        description="Unique passed-course units within this term."
    )
    cumulative_gwa: Decimal | None = Field(
        description="GWA from eligible attempts in completed terms up to this term; "
        "computed from unrounded totals, independently of pagination."
    )
    total_units_earned: Decimal = Field(
        description="Running earned units through this term, counting each passed "
        "catalog course only once across history."
    )
    completed: bool = Field(
        description="True when the term end date is before today's Asia/Manila date. "
        "Ongoing terms are visible but excluded from cumulative GWA."
    )


class TranscriptRead(BaseModel):
    """Paginated term history with whole-record totals.

    Every finalized attempt remains visible. All eligible attempts count
    toward GWA, but each passed catalog course earns cumulative credit
    only once. Decimal values are strings, not floating-point numbers.
    """

    student: RecordIdentity
    terms: list[TranscriptTerm] = Field(
        default_factory=list, description="This page's term blocks, newest first."
    )
    cumulative_gwa: Decimal | None = Field(
        default=None,
        description="Whole-record completed-term GWA as a two-place Decimal string. "
        "Null with no eligible numeric units; independent of the requested page.",
        examples=["2.21"],
    )
    total_units_earned: Decimal = Field(
        default=Decimal("0.0"),
        description="Whole-record earned units as a Decimal string, counting each "
        "passed catalog course once. Includes passes in ongoing terms.",
    )
    page: int = Field(description="Requested one-based term page.")
    limit: int = Field(
        description="Requested term-page size, default 20 and maximum 100."
    )
    total: int = Field(
        description="Number of terms with published marks across all pages."
    )
    next_page: int | None = Field(
        default=None,
        serialization_alias="nextPage",
        description="Next term page number, or null when no later page exists.",
    )
    state: Literal["available", "empty"] = Field(
        default="empty",
        description="Whole-record availability. Remains available for an out-of-range "
        "page when other pages contain marks.",
    )
    message: str | None = Field(
        default="No finalized marks are available.",
        description="Explanation when no posted marks exist anywhere in the record.",
    )


class SectionGradeEntryRead(BaseModel):
    """One enrolled student's grade row for a section."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )

    enrollment_id: uuid.UUID
    student_id: uuid.UUID
    student_number: str
    student_name: str
    raw_score: Decimal | None = None
    midterm_grade: str | None = None
    final_grade: str | None = None
    remarks: str | None = None
    inc_expires_at: datetime | None = None
    completion_deadline: datetime | None = None
    is_draft: bool = True
    is_locked: bool = False
    status: Literal["MISSING", "DRAFT", "FINAL"]
    submitted_at: datetime | None = None


class SectionGradesRead(BaseModel):
    """Section grade sheet with enrolled marks, workflow state, version, deadline."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    section_id: uuid.UUID
    section_name: str
    course_code: str | None = None
    course_title: str | None = None
    grading_mode: Literal["NUMERIC", "PASS_FAIL"] = Field(
        default="NUMERIC",
        description="Section grading mode (NUMERIC or PASS_FAIL).",
    )
    allowed_marks: list[str] = Field(
        default_factory=lambda: list(NUMERIC_ALLOWED_MARKS),
        description="Allowed marks for the section's grading mode.",
    )
    state: Literal["DRAFT", "FINAL"]
    version: int
    deadline: datetime
    deadline_passed: bool
    submitted_at: datetime | None = None
    grades: list[SectionGradeEntryRead] = Field(default_factory=list)


class GradeDeadlineRead(BaseModel):
    """Grade submission deadline and whether it has passed."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    section_id: uuid.UUID
    academic_term_id: uuid.UUID
    deadline: datetime
    passed: bool


class GradeDraftItemInput(BaseModel):
    """One student's draft mark input; validated in the service layer."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        json_schema_extra={
            "example": {
                "enrollmentId": "00000000-0000-0000-0000-000000000001",
                "finalGrade": "1.25",
                "midtermGrade": "1.50",
                "remarks": "Draft mark",
            }
        },
    )

    enrollment_id: uuid.UUID | None = Field(
        default=None,
        description="Enrolled student's course enrollment ID.",
        examples=["00000000-0000-0000-0000-000000000001"],
    )
    student_id: uuid.UUID | None = Field(
        default=None,
        description="Optional student user ID (omit when enrollmentId is provided).",
    )
    final_grade: str | int | float | Decimal | None = Field(
        default=None,
        validation_alias=AliasChoices("finalGrade", "final_grade", "mark"),
        description="Final grade mark (1.00 to 5.00 or INC for NUMERIC; "
        "PASS or FAIL for PASS_FAIL).",
        examples=["1.25"],
    )
    raw_score: Decimal | int | float | str | None = Field(
        default=None,
        description="Optional numeric raw score.",
        examples=["88.5"],
    )
    midterm_grade: str | None = Field(
        default=None,
        max_length=10,
        description="Optional midterm grade.",
        examples=["1.50"],
    )
    remarks: str | None = Field(
        default=None,
        max_length=100,
        description="Optional remarks for this grade entry.",
        examples=["Draft mark"],
    )


class SaveGradeDraftRequest(BaseModel):
    """Versioned draft save payload for a section's grade sheet."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        json_schema_extra={
            "example": {
                "version": 0,
                "grades": [
                    {
                        "enrollmentId": "00000000-0000-0000-0000-000000000001",
                        "finalGrade": "1.25",
                        "midtermGrade": "1.50",
                        "remarks": "Draft mark",
                    }
                ],
            }
        },
    )

    version: int = Field(
        ...,
        description="Expected version of draft submission for optimistic locking.",
        examples=[0],
    )
    grades: list[GradeDraftItemInput]


class FinalizeGradesRequest(BaseModel):
    """Optional optimistic-lock version check when finalizing a section."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        json_schema_extra={
            "example": {
                "version": 0,
            }
        },
    )

    version: int | None = Field(
        default=None,
        description="Optional expected version of the grade sheet before finalizing.",
        examples=[0],
    )


class FinalizeGradesRead(BaseModel):
    """Summary returned after finalizing and locking a section's grades."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    section_id: uuid.UUID
    state: Literal["FINAL"]
    version: int
    records_committed: int
    notifications_queued: int
    submitted_at: datetime


class IncompleteCompletionSummaryRead(BaseModel):
    """Compact summary of an Incomplete completion filing attached to an Inc row."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )

    id: uuid.UUID
    grade_id: uuid.UUID
    proposed_mark: str
    completion_form_file_id: str
    remarks: str | None = None
    state: Literal["PENDING_DEAN", "ENDORSED", "REJECTED", "EXPIRED"]
    version: int
    reviewed_by: uuid.UUID | None = None
    review_remarks: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime


class IncompleteGradeRead(BaseModel):
    """One student's Incomplete grade record with deadline, days left, and state."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    inc_id: uuid.UUID
    grade_id: uuid.UUID
    enrollment_id: uuid.UUID
    section_id: uuid.UUID
    section_name: str
    course_id: uuid.UUID
    course_code: str
    course_title: str
    academic_term_id: uuid.UUID
    academic_year: str
    semester: str
    student_id: uuid.UUID
    student_number: str
    student_name: str
    final_grade: str
    submitted_at: datetime | None = None
    completion_deadline: datetime | None = None
    days_left: int | None = None
    state: Literal["PENDING_COMPLETION", "COMPLETED", "EXPIRED"]
    latest_completion: IncompleteCompletionSummaryRead | None = None


class CreateIncompleteCompletionRequest(BaseModel):
    """Faculty filing of an Incomplete completion grade; validated in the service."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        json_schema_extra={
            "example": {
                "proposedMark": "1.50",
                "completionFormFileId": "form-inc-0001",
                "remarks": "Completed final project",
            }
        },
    )

    proposed_mark: str | int | float | Decimal | None = Field(
        default=None,
        validation_alias=AliasChoices("proposedMark", "proposed_mark", "mark"),
        description="Passing completion grade mark from 1.00 to 3.00.",
        examples=["1.50"],
    )
    completion_form_file_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "completionFormFileId", "completion_form_file_id", "formFileId"
        ),
        description="Reference identifier of the uploaded completion form.",
        examples=["form-inc-0001"],
    )
    remarks: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional remarks accompanying the completion filing.",
    )


class IncompleteCompletionRead(BaseModel):
    """Full Incomplete completion record returned to faculty and College Deans."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: uuid.UUID
    completion_id: uuid.UUID
    inc_id: uuid.UUID
    grade_id: uuid.UUID
    enrollment_id: uuid.UUID
    section_id: uuid.UUID
    section_name: str
    course_id: uuid.UUID
    course_code: str
    course_title: str
    academic_term_id: uuid.UUID
    academic_year: str
    semester: str
    student_id: uuid.UUID
    student_number: str
    student_name: str
    college: str | None = None
    proposed_mark: str
    completion_form_file_id: str
    remarks: str | None = None
    state: Literal["PENDING_DEAN", "ENDORSED", "REJECTED", "EXPIRED"]
    version: int
    reviewed_by: uuid.UUID | None = None
    review_remarks: str | None = None
    resolved_at: datetime | None = None
    completion_deadline: datetime | None = None
    created_at: datetime


class IncompleteCompletionDecisionRequest(BaseModel):
    """College Dean endorsement or rejection of an Incomplete completion."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    decision: Literal["ENDORSE", "REJECT"]
    remarks: str | None = Field(
        default=None,
        max_length=2000,
        validation_alias=AliasChoices("remarks", "reviewRemarks", "review_remarks"),
    )
    version: int | None = None


class IncExpiryRunResult(BaseModel):
    """Summary returned by one idempotent daily INC expiry sweep."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    run_key: str
    processed: int
    converted: int
    skipped_duplicate_run: bool = False
