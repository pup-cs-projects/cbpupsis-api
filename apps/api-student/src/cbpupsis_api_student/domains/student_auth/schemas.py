"""DTOs for student sign-in (US-101)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from cbpupsis_shared.domains.auth.constants import PASSWORD_MAX_LENGTH
from cbpupsis_shared.domains.auth.schemas import TokenPair


class StudentLoginRequest(BaseModel):
    """Student number, birthdate, and password.

    The student number's format is checked by the service, not here, so a
    malformed number answers ``AUTH_ID_FORMAT_INVALID`` like the other sign-in
    rules rather than a generic schema error.
    """

    model_config = ConfigDict(extra="forbid")

    student_number: str = Field(
        max_length=50,
        description="Institutional student number, YYYY-NNNNN-XX-N.",
        examples=["2021-00123-MN-0"],
    )
    birthdate: date | None = Field(
        default=None, description="Birthdate on the Registrar record (YYYY-MM-DD)."
    )
    password: str = Field(max_length=PASSWORD_MAX_LENGTH)


class StudentTokenPair(TokenPair):
    """A student session's tokens. The refresh token is also set as a cookie."""

    role: Literal["student"] = "student"
    expires_in: int


class StudentSessionRead(BaseModel):
    """The signed-in student, as returned by ``/auth/student/me``."""

    id: uuid.UUID
    email: EmailStr
    role: Literal["student"] = "student"
