"""Enrollment DTOs, without another student's data."""

import uuid

from pydantic import BaseModel, ConfigDict


class RecordEnrollment(BaseModel):
    id: uuid.UUID
    academic_term_id: uuid.UUID
    section_id: uuid.UUID


class EnrolledSection(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    section_id: uuid.UUID
    status: str
