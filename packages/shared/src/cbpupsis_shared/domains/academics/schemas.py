"""Term metadata shared by academic record readers."""

import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict


class TermRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    academic_year: str
    semester: str
    start_date: date
    end_date: date
