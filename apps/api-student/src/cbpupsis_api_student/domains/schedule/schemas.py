"""CamelCase public schedule contract; contact hours are weekly."""

import uuid
from datetime import date, time
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ScheduleModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ScheduleTerm(ScheduleModel):
    id: uuid.UUID
    year: str
    semester: str
    start_date: date
    end_date: date


class ScheduleMeeting(ScheduleModel):
    id: uuid.UUID
    day_of_week: int = Field(ge=0, le=6, description="Monday=0, Sunday=6")
    start_time: time
    end_time: time
    room: str | None
    type: Literal["lecture", "laboratory"]


class ScheduleCourse(ScheduleModel):
    code: str
    title: str
    section: str
    section_id: uuid.UUID
    room: str | None
    faculty_name: str | None
    units: Decimal
    lecture_hours: Decimal
    lab_hours: Decimal
    status: str
    meetings: list[ScheduleMeeting]


class ScheduleRead(ScheduleModel):
    student_name: str
    student_number: str
    term: ScheduleTerm | None
    courses: list[ScheduleCourse] = Field(default_factory=list)
    total_credits: Decimal = Decimal(0)
    total_lecture_hours: Decimal = Decimal(0)
    total_lab_hours: Decimal = Decimal(0)
    state: Literal["ready", "notEnrolled", "noMatches", "termUnavailable"]
    message: str | None = None
