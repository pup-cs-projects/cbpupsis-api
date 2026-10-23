"""Pydantic schemas (DTOs) for the student_profile domain (US-STU-002)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel


class ProfileFieldEntry(BaseModel):
    """One profile field with its value and self-service editability flag."""

    value: str | int | None
    editable: bool


class EmergencyContactSummaryRead(BaseModel):
    """Minimised emergency contact shown on the main profile read (BR-AUTH-007).

    Deliberately omits ``address`` and ``email``: those are served only on the
    dedicated emergency-contact edit endpoints.
    """

    id: uuid.UUID
    name: str
    relationship: str
    phone: str
    isPrimary: bool


class EmergencyContactDetailRead(BaseModel):
    """Full emergency contact returned on the dedicated contact edit endpoints."""

    id: uuid.UUID
    name: str
    relationship: str
    phone: str
    email: str | None = None
    address: str | None = None
    isPrimary: bool
    message: str | None = None


class StudentProfileRead(BaseModel):
    """Student profile response with per-field editability and minimised contacts."""

    studentId: str
    avatarUrl: str | None = None
    petitionRoute: str
    fields: dict[str, ProfileFieldEntry]
    emergencyContacts: list[EmergencyContactSummaryRead]
    message: str | None = None


class AvatarUploadRead(BaseModel):
    """Confirmation returned when a student uploads a valid profile picture."""

    avatarUrl: str
    url: str
    contentType: str
    sizeBytes: int
    message: str = "Profile picture uploaded successfully."
