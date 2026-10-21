"""Enrollment DTOs."""

import uuid

from pydantic import BaseModel, ConfigDict


class EnrolledSection(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    section_id: uuid.UUID
    status: str
