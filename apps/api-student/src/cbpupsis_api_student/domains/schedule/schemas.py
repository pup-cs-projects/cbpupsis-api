"""Snake_case public schedule contract; contact hours are weekly."""

import uuid
from datetime import date, time
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class ScheduleModel(BaseModel):
    """Serialize public property names exactly as declared by each DTO."""


class ScheduleTerm(ScheduleModel):
    """Configured academic term selected for this schedule."""

    id: uuid.UUID = Field(description="Academic term identifier.")
    year: str = Field(
        description="Stored academic-year string.", examples=["2026-2027"]
    )
    semester: str = Field(description="Stored semester identifier.", examples=["first"])
    start_date: date = Field(description="Inclusive term start date.")
    end_date: date = Field(
        description="Inclusive term end date and recurrence boundary."
    )


class ScheduleMeeting(ScheduleModel):
    """One weekly lecture or laboratory meeting, using Asia/Manila local times."""

    id: uuid.UUID = Field(description="Distinct meeting identifier within the section.")
    day_of_week: int = Field(ge=0, le=6, description="Monday=0, Sunday=6")
    start_time: time = Field(
        description="Meeting start time in Asia/Manila.", examples=["08:00:00"]
    )
    end_time: time = Field(
        description="Meeting end time in Asia/Manila.", examples=["10:00:00"]
    )
    room: str | None = Field(
        description="Meeting-specific room, or null if unassigned.", examples=["R101"]
    )
    type: Literal["lecture", "laboratory"] = Field(
        description="Contact-hour category for this meeting."
    )


class ScheduleCourse(ScheduleModel):
    """An officially enrolled course and its section's complete weekly timetable."""

    code: str = Field(
        description="Course code; searchable using q.", examples=["CS001"]
    )
    title: str = Field(
        description="Course title; searchable using q.", examples=["Algorithms"]
    )
    section: str = Field(description="Enrolled section name.", examples=["BSCS 1-1"])
    section_id: uuid.UUID = Field(description="Enrolled section identifier.")
    room: str | None = Field(
        description="Distinct meeting rooms joined by commas, or null."
    )
    faculty_name: str | None = Field(
        description="Assigned faculty name; searchable using q; null if unassigned."
    )
    units: Decimal = Field(
        description="Credit units, serialized as a decimal string; not contact hours."
    )
    lecture_hours: Decimal = Field(
        description="Weekly lecture hours from meeting durations, as a decimal string."
    )
    lab_hours: Decimal = Field(
        description="Weekly lab hours from meeting durations, as a decimal string."
    )
    status: str = Field(
        description="Official enrollment status; only enrolled courses are included.",
        examples=["enrolled"],
    )
    meetings: list[ScheduleMeeting] = Field(
        description="All weekly meeting slots, preserving individual times and rooms."
    )


class ScheduleRead(ScheduleModel):
    """Direct snake_case schedule response; no status/data success envelope."""

    student_name: str = Field(description="Signed-in student's full name.")
    student_number: str = Field(
        description="Signed-in student's institutional number.",
        examples=["2021-00123-MN-0"],
    )
    term: ScheduleTerm | None = Field(
        description="Selected term, or null when configuration is unavailable."
    )
    courses: list[ScheduleCourse] = Field(
        default_factory=list,
        description="Officially enrolled courses matching q; empty for empty states.",
    )
    total_credits: Decimal = Field(
        default=Decimal(0),
        description="Sum of returned course credit units, as a decimal string.",
    )
    total_lecture_hours: Decimal = Field(
        default=Decimal(0),
        description="Sum of returned weekly lecture hours, as a decimal string.",
    )
    total_lab_hours: Decimal = Field(
        default=Decimal(0),
        description="Sum of returned weekly laboratory hours, as a decimal string.",
    )
    state: Literal["ready", "notEnrolled", "noMatches", "termUnavailable"] = Field(
        description=(
            "ready: courses returned; notEnrolled: no official enrollment; "
            "noMatches: filter matched none; termUnavailable: missing/ambiguous "
            "term configuration."
        )
    )
    message: str | None = Field(
        default=None,
        description="Human-readable explanation for an empty result; null when ready.",
    )
