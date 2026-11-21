"""DTOs for the sections domain."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict


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
