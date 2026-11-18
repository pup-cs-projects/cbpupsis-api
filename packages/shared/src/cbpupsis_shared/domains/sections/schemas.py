"""DTOs for the sections domain."""

from __future__ import annotations

import uuid
from datetime import time
from enum import StrEnum
from typing import Literal

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
    capacity: int
    status: str
    meetings: list[MeetingSlot]


class CourseSectionRead(BaseModel):
    """One course section assigned to the caller."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    section_name: str
    capacity: int
    status: str


class OwnedSectionRead(BaseModel):
    """Section metadata for an owned section returned across domain boundaries."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    academic_term_id: uuid.UUID
    course_id: uuid.UUID
    faculty_id: uuid.UUID | None
    section_name: str
    capacity: int
    status: str


class EnrollmentStatus(StrEnum):
    ENROLLED = "ENROLLED"
    DROPPED = "DROPPED"
    WITHDRAWN = "WITHDRAWN"


class GradeStatus(StrEnum):
    PENDING = "PENDING"
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    INC = "INC"


class AddDropStatus(StrEnum):
    NONE = "NONE"
    PENDING_ADD = "PENDING_ADD"
    PENDING_DROP = "PENDING_DROP"
    APPROVED_ADD = "APPROVED_ADD"
    APPROVED_DROP = "APPROVED_DROP"


class RosterEntryRead(BaseModel):
    """One enrolled student on a roster."""

    model_config = ConfigDict(populate_by_name=True)

    user_id: uuid.UUID
    student_number: str = Field(serialization_alias="studentNumber")
    name: str
    email: str
    enrollment_status: EnrollmentStatus = Field(serialization_alias="enrollmentStatus")
    grade_status: GradeStatus = Field(serialization_alias="gradeStatus")
    add_drop_status: AddDropStatus = Field(serialization_alias="addDropStatus")


class CourseSectionRosterRead(CourseSectionRead):
    """A filtered page of students in a section owned by the caller."""

    roster: list[RosterEntryRead] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class FacultyStudentRead(BaseModel):
    """Teaching and contact data; deliberately excludes private profile data."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    student_number: str = Field(serialization_alias="studentNumber")
    email: str
    program: str
    year_level: int = Field(serialization_alias="yearLevel")


class FacultyCourseAttemptRead(BaseModel):
    """One published attempt relevant to the section's course."""

    model_config = ConfigDict(populate_by_name=True)

    enrollment_id: uuid.UUID = Field(serialization_alias="enrollmentId")
    academic_term_id: uuid.UUID = Field(serialization_alias="academicTermId")
    academic_year: str = Field(serialization_alias="academicYear")
    semester: str
    final_grade: str = Field(serialization_alias="finalGrade")
    result: Literal["PASSED", "FAILED", "INCOMPLETE", "WITHDRAWN"]


class FacultyPrerequisiteRecordRead(BaseModel):
    """Published outcomes for one prerequisite of the taught course."""

    model_config = ConfigDict(populate_by_name=True)

    course_id: uuid.UUID = Field(serialization_alias="courseId")
    course_code: str = Field(serialization_alias="courseCode")
    title: str
    result: Literal["PASSED", "NOT_PASSED", "NOT_TAKEN"]
    attempts: list[FacultyCourseAttemptRead] = Field(default_factory=list)


class FacultyStudentRecordRead(BaseModel):
    """Course-scoped academic record disclosed to the teaching faculty member."""

    model_config = ConfigDict(populate_by_name=True)

    course_id: uuid.UUID = Field(serialization_alias="courseId")
    course_attempts: list[FacultyCourseAttemptRead] = Field(
        default_factory=list, serialization_alias="courseAttempts"
    )
    prerequisites: list[FacultyPrerequisiteRecordRead] = Field(default_factory=list)


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
