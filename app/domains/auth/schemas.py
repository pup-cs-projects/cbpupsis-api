"""Pydantic schemas (DTOs) for the auth domain."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.domains.auth.constants import (
    ONE_TIME_TOKEN_MAX_LENGTH,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
)
from app.domains.auth.models import AdminPosition
from app.domains.users.constants import FULL_NAME_MAX_LENGTH


class RegisterRequest(BaseModel):
    """Payload for creating an account."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    # Length is the property that actually resists guessing; composition rules
    # ("one symbol, one digit") mostly push users toward predictable patterns.
    password: str = Field(
        min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH
    )
    full_name: str | None = Field(default=None, max_length=FULL_NAME_MAX_LENGTH)


class LoginRequest(BaseModel):
    """Payload for exchanging credentials for tokens."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: str = Field(max_length=PASSWORD_MAX_LENGTH)


class RefreshRequest(BaseModel):
    """Payload for exchanging a refresh token for a new token pair."""

    model_config = ConfigDict(extra="forbid")

    refresh_token: str


class TokenPair(BaseModel):
    """An access/refresh token pair, as returned by login and refresh."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class AdminLoginChallenge(BaseModel):
    """Password-step result for an administrator; never an API session."""

    mfa_required: Literal[True] = True
    challenge_token: str
    enrollment_required: bool
    methods: list[Literal["totp", "webauthn"]]
    webauthn_challenge: str
    webauthn_rp_id: str
    webauthn_credential_id: str | None = None


class AdminTokenPair(TokenPair):
    """An MFA-completed administrative session response."""

    role: Literal["admin"] = "admin"
    position: AdminPosition
    department_id: str | None = None
    college_id: str | None = None


class MfaVerifyRequest(BaseModel):
    """Complete an admin challenge with exactly one supported factor."""

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
    """Start TOTP enrollment using the restricted login challenge."""

    model_config = ConfigDict(extra="forbid")

    challenge_token: str


class TotpEnrollmentRead(BaseModel):
    """TOTP seed returned once for entry or QR rendering by the client."""

    secret: str
    provisioning_uri: str


class TotpEnrollmentConfirmRequest(BaseModel):
    """Prove the newly enrolled TOTP seed before activating it."""

    model_config = ConfigDict(extra="forbid")

    challenge_token: str
    code: str = Field(min_length=6, max_length=6)


class AdminProfileConfigureRequest(BaseModel):
    """Assign one admin position and its data scope."""

    model_config = ConfigDict(extra="forbid")

    position: AdminPosition
    department_id: str | None = Field(default=None, max_length=100)
    college_id: str | None = Field(default=None, max_length=100)


class CurrentUserRead(BaseModel):
    """The authenticated principal, as returned by /whoami."""

    id: uuid.UUID
    email: EmailStr


class VerifyEmailRequest(BaseModel):
    """Payload for redeeming an email-verification token."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(max_length=ONE_TIME_TOKEN_MAX_LENGTH)


class ResendVerificationRequest(BaseModel):
    """Payload for requesting a fresh verification email.

    Takes an email rather than requiring authentication, so a user who cannot
    finish signup on the device they registered on is not locked out of the
    flow. Like forgot-password, the response is identical whether or not the
    address exists.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    """Payload for requesting a password-reset email."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """Payload for redeeming a reset token and setting a new password."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(max_length=ONE_TIME_TOKEN_MAX_LENGTH)
    new_password: str = Field(
        min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH
    )


class ChangePasswordRequest(BaseModel):
    """Payload for an authenticated password change.

    The current password is required even though the caller holds a valid access
    token: without it, a stolen token could be used to lock the real owner out
    by changing the credential.
    """

    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(max_length=PASSWORD_MAX_LENGTH)
    new_password: str = Field(
        min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH
    )
