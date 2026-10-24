"""Pydantic schemas (DTOs) for the curriculum domain."""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ProgramRead(BaseModel):
    """A degree program, as other domains see it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    department: str
    is_active: bool


class RecordCourse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    course_code: str
    title: str
    total_units: Decimal


class ScheduleCourse(BaseModel):
    """Course catalog fields used by student schedules."""

    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    course_code: str
    title: str
    lecture_units: Decimal
    lab_units: Decimal
