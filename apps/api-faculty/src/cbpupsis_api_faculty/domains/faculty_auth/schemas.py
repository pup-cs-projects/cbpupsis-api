"""DTOs for faculty sign-in and MFA (US-102)."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from cbpupsis_shared.domains.auth.constants import PASSWORD_MAX_LENGTH
from cbpupsis_shared.domains.auth.schemas import TokenPair


class FacultyLoginRequest(BaseModel):
    """Employee id and password. The id's format is checked by the service."""

    model_config = ConfigDict(extra="forbid")

    employee_id: str = Field(
        max_length=50,
        description="Institutional employee id, YYYY-NNNNN-XX-N.",
        examples=["2021-00456-MN-0"],
    )
    password: str = Field(max_length=PASSWORD_MAX_LENGTH)


class FacultyLoginChallenge(BaseModel):
    """The password step's only result: a challenge that grants no API access."""

    mfa_required: Literal[True] = True
    challenge_token: str
    enrollment_required: bool
    methods: list[Literal["totp"]]


class ChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_token: str


class TotpEnrollmentRead(BaseModel):
    """A pending seed: the secret for manual entry and the URI to scan."""

    secret: str
    provisioning_uri: str


class TotpConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_token: str
    code: str = Field(min_length=6, max_length=6)


class FacultyMfaVerifyRequest(BaseModel):
    """Exactly one second factor: a TOTP code or an unused recovery code."""

    model_config = ConfigDict(extra="forbid")

    challenge_token: str
    code: str | None = Field(default=None, min_length=6, max_length=6)
    recovery_code: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def exactly_one_factor(self) -> FacultyMfaVerifyRequest:
        if (self.code is None) == (self.recovery_code is None):
            raise ValueError("provide exactly one of code or recovery_code")
        return self


class FacultyTokenPair(TokenPair):
    """A faculty session's tokens. The refresh token is also set as a cookie."""

    role: Literal["faculty"] = "faculty"
    expires_in: int


class FacultyEnrollmentResult(FacultyTokenPair):
    """The first session after enrollment, with recovery codes shown once."""

    recovery_codes: list[str]


class FacultySessionRead(BaseModel):
    """The signed-in faculty member, as returned by ``/auth/faculty/me``."""

    id: uuid.UUID
    email: EmailStr
    employee_id: str | None
    role: Literal["faculty"] = "faculty"
