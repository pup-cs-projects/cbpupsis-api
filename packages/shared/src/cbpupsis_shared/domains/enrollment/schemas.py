"""Minimal enrolled-attempt identifiers, without another student's data."""

import uuid

from pydantic import BaseModel


class RecordEnrollment(BaseModel):
    id: uuid.UUID
    academic_term_id: uuid.UUID
    section_id: uuid.UUID
