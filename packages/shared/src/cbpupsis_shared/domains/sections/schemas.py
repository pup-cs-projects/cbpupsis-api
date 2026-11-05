"""DTOs for the sections domain."""

from __future__ import annotations

import uuid
from datetime import time

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

_DAY_NAMES: dict[str, int] = {
    "MONDAY": 0,
    "MON": 0,
    "M": 0,
    "TUESDAY": 1,
    "TUE": 1,
    "TUES": 1,
    "T": 1,
    "WEDNESDAY": 2,
    "WED": 2,
    "W": 2,
    "THURSDAY": 3,
    "THU": 3,
    "THUR": 3,
    "THURS": 3,
    "TH": 3,
    "R": 3,
    "FRIDAY": 4,
    "FRI": 4,
    "F": 4,
    "SATURDAY": 5,
    "SAT": 5,
    "S": 5,
    "SUNDAY": 6,
    "SUN": 6,
    "SU": 6,
    "U": 6,
}


class RecordSection(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    course_id: uuid.UUID
    academic_term_id: uuid.UUID


class MeetingSlot(BaseModel):
    """Weekly meeting; weekdays use Python's Monday=0 convention."""

    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    day_of_week: int
    start_time: time
    end_time: time
    room: str | None
    schedule_type: str


class ScheduleSection(BaseModel):
    id: uuid.UUID
    academic_term_id: uuid.UUID
    course_id: uuid.UUID
    faculty_id: uuid.UUID | None
    section_name: str
    meetings: list[MeetingSlot]


class CourseSectionRead(BaseModel):
    """One course section assigned to the caller."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    section_name: str
    capacity: int
    status: str


class RosterEntryRead(BaseModel):
    """One enrolled student on a roster."""

    user_id: uuid.UUID


class CourseSectionRosterRead(CourseSectionRead):
    """A section with its roster.

    The roster stays empty until the enrollment domain exists to fill it; the
    endpoint already enforces who may see it.
    """

    roster: list[RosterEntryRead] = []


class SectionMeetingInput(BaseModel):
    """One meeting slot in a section create or update request."""

    model_config = ConfigDict(populate_by_name=True)

    day_of_week: int = Field(
        default=0,
        ge=0,
        le=6,
        validation_alias=AliasChoices("dayOfWeek", "day_of_week", "day"),
    )
    start_time: time = Field(
        default=time(8, 0),
        validation_alias=AliasChoices("startTime", "start_time", "start"),
    )
    end_time: time = Field(
        default=time(10, 0),
        validation_alias=AliasChoices("endTime", "end_time", "end"),
    )
    room: str | None = None
    schedule_type: str = Field(
        default="lecture",
        validation_alias=AliasChoices("scheduleType", "schedule_type", "type"),
    )

    @field_validator("day_of_week", mode="before")
    @classmethod
    def normalize_day_of_week(cls, value: object) -> object:
        if isinstance(value, str):
            cleaned = value.strip().upper()
            if cleaned.isdigit():
                return int(cleaned)
            if cleaned in _DAY_NAMES:
                return _DAY_NAMES[cleaned]
        return value


class AdminSectionCreate(BaseModel):
    """Payload to create a course section in the caller's department."""

    model_config = ConfigDict(populate_by_name=True)

    course_id: uuid.UUID = Field(validation_alias=AliasChoices("courseId", "course_id"))
    academic_term_id: uuid.UUID | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "academicTermId", "academic_term_id", "termId", "term_id"
        ),
    )
    section_name: str = Field(
        default="1-A",
        min_length=1,
        max_length=30,
        validation_alias=AliasChoices(
            "sectionName", "section_name", "sectionCode", "section_code", "name"
        ),
    )
    faculty_id: uuid.UUID | None = Field(
        default=None,
        validation_alias=AliasChoices("facultyId", "faculty_id", "faculty"),
    )
    capacity: int = Field(ge=1)
    room: str | None = None
    status: str = Field(default="open", max_length=20)
    meetings: list[SectionMeetingInput] = Field(
        default_factory=list,
        validation_alias=AliasChoices("meetings", "schedules", "schedule"),
    )


class AdminSectionUpdate(BaseModel):
    """Payload to edit an existing course section."""

    model_config = ConfigDict(populate_by_name=True)

    section_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=30,
        validation_alias=AliasChoices(
            "sectionName", "section_name", "sectionCode", "section_code", "name"
        ),
    )
    faculty_id: uuid.UUID | None = Field(
        default=None,
        validation_alias=AliasChoices("facultyId", "faculty_id", "faculty"),
    )
    capacity: int | None = Field(default=None, ge=0)
    room: str | None = None
    status: str | None = Field(default=None, max_length=20)
    meetings: list[SectionMeetingInput] | None = Field(
        default=None,
        validation_alias=AliasChoices("meetings", "schedules", "schedule"),
    )


class SectionMeetingRead(BaseModel):
    """Weekly meeting slot returned by admin section endpoints."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    day_of_week: int = Field(serialization_alias="dayOfWeek")
    start_time: time = Field(serialization_alias="startTime")
    end_time: time = Field(serialization_alias="endTime")
    room: str | None = None
    schedule_type: str = Field(serialization_alias="scheduleType")


class AdminSectionRead(BaseModel):
    """Course section representation returned by admin catalog endpoints."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    course_id: uuid.UUID = Field(serialization_alias="courseId")
    academic_term_id: uuid.UUID = Field(serialization_alias="academicTermId")
    faculty_id: uuid.UUID | None = Field(default=None, serialization_alias="facultyId")
    section_name: str = Field(serialization_alias="sectionName")
    capacity: int
    enrolled: int = 0
    seats_left: int = Field(default=0, serialization_alias="seatsLeft")
    room: str | None = None
    status: str
    meetings: list[SectionMeetingRead] = Field(default_factory=list)
