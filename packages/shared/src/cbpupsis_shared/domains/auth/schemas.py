"""Pydantic schemas (DTOs) for the auth domain."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
)

from cbpupsis_shared.domains.auth.constants import (
    FORGOT_PASSWORD_SUCCESS_MESSAGE,
    ONE_TIME_TOKEN_MAX_LENGTH,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    RESET_PASSWORD_MIN_LENGTH,
)
from cbpupsis_shared.domains.users.constants import FULL_NAME_MAX_LENGTH


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

    email: Any = Field(json_schema_extra={"type": "string", "format": "email"})


class ForgotPasswordResponse(BaseModel):
    """Uniform response for a password-reset request."""

    message: str = FORGOT_PASSWORD_SUCCESS_MESSAGE


class ResetPasswordRequest(BaseModel):
    """Payload for redeeming a reset token and setting a new password."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(max_length=ONE_TIME_TOKEN_MAX_LENGTH)
    new_password: str


class ResetPasswordResponse(BaseModel):
    """Response payload for a successful password reset."""

    model_config = ConfigDict(populate_by_name=True)

    sessions_revoked: int = Field(
        alias="sessionsRevoked", serialization_alias="sessionsRevoked"
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


class VerifyResetTokenRequest(BaseModel):
    """Non-consuming check on the reset endpoint before displaying the form."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(max_length=ONE_TIME_TOKEN_MAX_LENGTH)
    verify_only: Literal[True]


class ResetTokenVerificationResponse(BaseModel):
    """Policy information for a valid reset link; does not consume the token."""

    valid: bool = True
    minimumLength: int = RESET_PASSWORD_MIN_LENGTH
    requiredRules: list[str] = Field(
        default_factory=lambda: ["uppercase", "lowercase", "digit", "symbol"]
    )
