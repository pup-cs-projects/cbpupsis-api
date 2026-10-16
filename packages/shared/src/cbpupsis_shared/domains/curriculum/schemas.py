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
