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
    college: str | None = None
    is_active: bool


class CurriculumRead(BaseModel):
    """A versioned curriculum specification, as other domains see it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    program_id: uuid.UUID
    curriculum_year: str
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


class AcademicCourse(BaseModel):
    """Reviewed catalogue classification; ``None`` means not yet reviewed."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    course_code: str
    total_units: Decimal
    pe_sequence: int | None
    is_nstp: bool | None


class PrerequisiteEdge(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    course_id: uuid.UUID
    prerequisite_course_id: uuid.UUID


class AcademicCatalog(BaseModel):
    courses: list[AcademicCourse]
    prerequisites: list[PrerequisiteEdge]
    placements: dict[uuid.UUID, int]
