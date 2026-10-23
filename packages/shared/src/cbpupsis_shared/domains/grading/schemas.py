"""Student record contracts; Decimal values serialize as strings, not floats."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from cbpupsis_shared.domains.academics.schemas import TermRead
from cbpupsis_shared.domains.grading.constants import AcademicStanding
from cbpupsis_shared.domains.users.schemas import RecordIdentity

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
    "Inc",
    "W",
]


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
        "Inc means incomplete and W means withdrawn.",
        examples=["1.25", "Inc", "W"],
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
