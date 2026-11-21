"""Pydantic schemas for the faculty_profile domain (US-FAC-005)."""

from __future__ import annotations

from pydantic import BaseModel


class ProfileFieldEntry(BaseModel):
    """One faculty profile field with its value and self-service editability flag."""

    value: str | None
    editable: bool


class FacultyProfileRead(BaseModel):
    """Faculty profile response with per-field editability flags."""

    petitionRoute: str
    fields: dict[str, ProfileFieldEntry]
    message: str | None = None
