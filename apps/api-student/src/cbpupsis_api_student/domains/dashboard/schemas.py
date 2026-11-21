"""Pydantic schemas (DTOs) for the student dashboard domain."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from cbpupsis_shared.domains.academics.schemas import (
    AcademicEventRead,
    AcademicPeriodRead,
    AcademicTermRead,
)
from cbpupsis_shared.domains.announcements.schemas import AnnouncementRead
from cbpupsis_shared.domains.audit.schemas import StudentRecentActivityRead
from cbpupsis_shared.domains.billing.schemas import HoldRead
from cbpupsis_shared.domains.enrollment.schemas import EnrollmentSummaryRead
from cbpupsis_shared.domains.grading.schemas import ScholasticStandingRead


class StudentIdentityRead(BaseModel):
    """The authenticated student's identity block."""

    model_config = ConfigDict(from_attributes=True)

    student_number: str
    name: str
    programme: str
    year_level: int
    enrollment_status: str
    avatar_url: str | None = None


class EmptyStatesRead(BaseModel):
    """Explanatory empty states for zero-history panels."""

    model_config = ConfigDict(from_attributes=True)

    announcements: str | None = None
    events: str | None = None
    recent_activity: str | None = None


class StudentDashboardData(BaseModel):
    """Aggregated sections of the student landing dashboard."""

    model_config = ConfigDict(from_attributes=True)

    student: StudentIdentityRead
    current_term: AcademicTermRead
    holds: list[HoldRead] = Field(default_factory=list)
    standing: ScholasticStandingRead
    enrollment: EnrollmentSummaryRead
    periods: list[AcademicPeriodRead] = Field(default_factory=list)
    announcements: list[AnnouncementRead] = Field(default_factory=list)
    events: list[AcademicEventRead] = Field(default_factory=list)
    recent_activity: list[StudentRecentActivityRead] = Field(default_factory=list)
    empty_states: EmptyStatesRead


class StudentDashboardResponse(BaseModel):
    """Root response envelope for the dashboard."""

    status: str = "success"
    data: StudentDashboardData
