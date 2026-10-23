"""Schemas for the student dashboard aggregate landing view."""

from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, computed_field

from cbpupsis_shared.domains.academics.schemas import (
    AcademicEventRead,
    AcademicPeriodRead,
)
from cbpupsis_shared.domains.announcements.schemas import AnnouncementRead
from cbpupsis_shared.domains.audit.schemas import StudentRecentActivityRead
from cbpupsis_shared.domains.billing.schemas import HoldRead
from cbpupsis_shared.domains.enrollment.schemas import EnrollmentSummaryRead
from cbpupsis_shared.domains.grading.schemas import ScholasticStandingRead


class StudentIdentityRead(BaseModel):
    student_id: str = Field(
        ...,
        validation_alias=AliasChoices("studentId", "student_id", "student_number"),
    )
    display_name: str = Field(
        ...,
        validation_alias=AliasChoices("displayName", "display_name", "name"),
    )
    program: str = Field(
        ...,
        validation_alias=AliasChoices("program", "programme"),
    )
    year_level: int = Field(
        ...,
        validation_alias=AliasChoices("yearLevel", "year_level"),
    )
    enrollment_status: str = Field(
        ...,
        validation_alias=AliasChoices("enrollmentStatus", "enrollment_status"),
    )
    avatar_url: str | None = Field(
        None,
        validation_alias=AliasChoices("avatarUrl", "avatar_url"),
    )

    @computed_field
    @property
    def studentId(self) -> str:
        return self.student_id

    @computed_field
    @property
    def student_number(self) -> str:
        return self.student_id

    @computed_field
    @property
    def displayName(self) -> str:
        return self.display_name

    @computed_field
    @property
    def name(self) -> str:
        return self.display_name

    @computed_field
    @property
    def programme(self) -> str:
        return self.program

    @computed_field
    @property
    def yearLevel(self) -> int:
        return self.year_level

    @computed_field
    @property
    def enrollmentStatus(self) -> str:
        return self.enrollment_status

    @computed_field
    @property
    def avatarUrl(self) -> str | None:
        return self.avatar_url

    model_config = ConfigDict(populate_by_name=True)


class TermRead(BaseModel):
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
        True,
        validation_alias=AliasChoices("isCurrent", "is_current"),
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

    model_config = ConfigDict(populate_by_name=True)


class EmptyStatesRead(BaseModel):
    announcements: str | None = None
    events: str | None = None
    recent_activity: str | None = Field(
        None,
        validation_alias=AliasChoices("recentActivity", "recent_activity"),
    )

    @computed_field
    @property
    def recentActivity(self) -> str | None:
        return self.recent_activity

    model_config = ConfigDict(populate_by_name=True)


class StudentDashboardData(BaseModel):
    student: StudentIdentityRead
    term: TermRead
    holds: list[HoldRead] = Field(default_factory=list)
    standing: ScholasticStandingRead
    enrollment: EnrollmentSummaryRead
    periods: list[AcademicPeriodRead] = Field(default_factory=list)
    announcements: list[AnnouncementRead] = Field(default_factory=list)
    events: list[AcademicEventRead] = Field(default_factory=list)
    recent_activity: list[StudentRecentActivityRead] = Field(
        default_factory=list,
        validation_alias=AliasChoices("recentActivity", "recent_activity"),
    )
    empty_states: EmptyStatesRead = Field(
        ...,
        validation_alias=AliasChoices("emptyStates", "empty_states"),
    )

    @computed_field
    @property
    def current_term(self) -> TermRead:
        return self.term

    @computed_field
    @property
    def recentActivity(self) -> list[StudentRecentActivityRead]:
        return self.recent_activity

    @computed_field
    @property
    def emptyStates(self) -> EmptyStatesRead:
        return self.empty_states

    model_config = ConfigDict(populate_by_name=True)


class StudentDashboardResponse(BaseModel):
    status: str = "success"
    data: StudentDashboardData

    model_config = ConfigDict(populate_by_name=True)
