"""DTOs for the sections domain."""

from __future__ import annotations

import uuid
from datetime import time

from pydantic import BaseModel, ConfigDict


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
