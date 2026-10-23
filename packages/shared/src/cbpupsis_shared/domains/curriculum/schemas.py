"""Pydantic schemas (DTOs) for the curriculum domain."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict


class ProgramRead(BaseModel):
    """A degree program, as other domains see it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    department: str
    is_active: bool


class CurriculumRead(BaseModel):
    """A versioned curriculum specification, as other domains see it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    program_id: uuid.UUID
    curriculum_year: str
    is_active: bool
