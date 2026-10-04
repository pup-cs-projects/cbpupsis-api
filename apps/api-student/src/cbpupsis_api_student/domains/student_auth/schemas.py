"""Pydantic schemas for the student authentication flow."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StudentLoginCredentials(BaseModel):
    """Internal credentials assembled by the student-auth repository."""

    user_id: uuid.UUID
    email: str
    password_hash: str
    is_active: bool
    anonymized_at: datetime | None = None
    birthdate: date | None = None


class StudentLoginRequest(BaseModel):
    """Payload submitted for student authentication."""

    model_config = ConfigDict(extra="ignore")

    student_number: str = Field(
        ...,
        description="Institutional student identifier in YYYY-NNNNN-XX-N format.",
        examples=["2021-00123-MN-0"],
    )
    birthdate: date | None = Field(
        default=None,
        description="Birthdate matching Registrar master record (YYYY-MM-DD).",
        examples=["2003-05-14"],
    )
    password: str = Field(
        ...,
        description="Student account password.",
    )


class StudentLoginResponseData(BaseModel):
    """Data object inside the login response envelope."""

    access_token: str
    expires_in: int = 900
    token_type: str = "bearer"
    role: str = "student"
    refresh_token: str | None = None


class SuccessEnvelope[T](BaseModel):
    """Standard success response envelope."""

    status: str = "success"
    data: T


class StudentLoginResponse(BaseModel):
    """Response envelope with nested data and root-level token fields."""

    status: str = "success"
    data: StudentLoginResponseData
    access_token: str
    token_type: str = "bearer"
    role: str = "student"
    refresh_token: str | None = None


class LockoutData(BaseModel):
    retry_after_seconds: int = 900


class ErrorEnvelope(BaseModel):
    """Standard error response envelope matching Issue #89."""

    status: str = "error"
    code: str
    message: str
    data: dict[str, Any] | None = None
