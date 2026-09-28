"""DTOs for the administrative two-step sign-in flow."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from cbpupsis_database.models.admin_auth import AdminPosition
from cbpupsis_shared.domains.auth.constants import PASSWORD_MAX_LENGTH
from cbpupsis_shared.domains.auth.schemas import RefreshRequest, TokenPair


class AdminLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(max_length=PASSWORD_MAX_LENGTH)


class AdminLoginChallenge(BaseModel):
    mfa_required: Literal[True] = True
    challenge_token: str
    enrollment_required: bool
    methods: list[Literal["totp", "webauthn"]]
    webauthn_challenge: str
    webauthn_rp_id: str
    webauthn_credential_id: str | None = None


class AdminTokenPair(TokenPair):
    role: Literal["admin"] = "admin"
    position: AdminPosition
    department_id: str | None = None
    college_id: str | None = None


class AdminSessionRead(BaseModel):
    role: Literal["admin"] = "admin"
    id: uuid.UUID
    email: EmailStr
    position: AdminPosition
    department_id: str | None = None
    college_id: str | None = None


class MfaVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_token: str
    code: str | None = Field(default=None, min_length=6, max_length=6)
    assertion: dict[str, Any] | None = None

    @model_validator(mode="after")
    def exactly_one_factor(self) -> MfaVerifyRequest:
        if (self.code is None) == (self.assertion is None):
            raise ValueError("provide exactly one of code or assertion")
        return self


class TotpEnrollmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_token: str


class TotpEnrollmentRead(BaseModel):
    secret: str
    provisioning_uri: str


class TotpEnrollmentConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_token: str
    code: str = Field(min_length=6, max_length=6)


class WebAuthnEnrollmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_token: str


class WebAuthnEnrollmentRead(BaseModel):
    public_key: dict[str, Any]


class WebAuthnEnrollmentConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_token: str
    credential: dict[str, Any]


class AdminProfileConfigureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: AdminPosition
    department_id: str | None = Field(default=None, max_length=100)
    college_id: str | None = Field(default=None, max_length=100)


__all__ = ["RefreshRequest"]
