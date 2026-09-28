"""Pydantic schemas (DTOs) for the users domain.

The API contract, split by direction: Base holds shared fields, Read adds
server-owned ones, Update carries partial edits. There is no ``UserCreate``
here — account creation is an authentication concern and lives in
``cbpupsis_shared.domains.auth.schemas.RegisterRequest``.

Note what is absent: ``password_hash`` appears in no schema, so it cannot leak
through a response by accident.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from cbpupsis_shared.domains.auth.constants import PASSWORD_MAX_LENGTH
from cbpupsis_shared.domains.users.constants import (
    AVATAR_URL_MAX_LENGTH,
    DISPLAY_NAME_MAX_LENGTH,
    E164_PATTERN,
    FULL_NAME_MAX_LENGTH,
    LOCALE_MAX_LENGTH,
    LOCALE_PATTERN,
    PHONE_NUMBER_MAX_LENGTH,
    TIMEZONE_MAX_LENGTH,
)


def _validate_timezone(value: str) -> str:
    """Reject anything the standard library does not know as an IANA zone.

    A free-text timezone is a bug waiting for the first render: storing
    "PST" or "GMT+2" produces a value nothing can convert with.
    """
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{value!r} is not a valid IANA timezone name") from exc
    return value


class UserBase(BaseModel):
    """Fields shared by user input and output."""

    email: EmailStr
    full_name: str | None = Field(default=None, max_length=FULL_NAME_MAX_LENGTH)


class UserProfileFields(BaseModel):
    """The editable profile fields, with their validation.

    Defined once and inherited by both the update schema and the onboarding
    check, so the two can never disagree about what a valid timezone is.
    """

    display_name: str | None = Field(
        default=None, min_length=1, max_length=DISPLAY_NAME_MAX_LENGTH
    )
    # Inline: the column is Text (unbounded), so this cap has no model-side
    # counterpart to drift from and no second reader to keep in step.
    bio: str | None = Field(default=None, max_length=2000)
    #: Kept a plain constrained string rather than ``HttpUrl`` because pydantic
    #: normalises HttpUrl (appending a trailing slash), which would silently
    #: rewrite a value the client sent and then read back differently.
    avatar_url: str | None = Field(default=None, max_length=AVATAR_URL_MAX_LENGTH)
    timezone: str | None = Field(default=None, max_length=TIMEZONE_MAX_LENGTH)
    locale: str | None = Field(default=None, max_length=LOCALE_MAX_LENGTH)
    phone_number: str | None = Field(default=None, max_length=PHONE_NUMBER_MAX_LENGTH)

    @field_validator("avatar_url")
    @classmethod
    def _check_avatar_url(cls, value: str | None) -> str | None:
        """Require an absolute http(s) URL.

        Rejecting other schemes matters: a stored ``javascript:`` value becomes
        stored XSS the moment a frontend renders it into an href.
        """
        if value is None:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("avatar_url must be an absolute http(s) URL")
        return value

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str | None) -> str | None:
        return None if value is None else _validate_timezone(value)

    @field_validator("locale")
    @classmethod
    def _check_locale(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not LOCALE_PATTERN.match(value):
            raise ValueError("locale must be a BCP 47 tag, e.g. 'en' or 'en-US'")
        return value

    @field_validator("phone_number")
    @classmethod
    def _check_phone_number(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not E164_PATTERN.match(value):
            raise ValueError(
                "phone_number must be in E.164 format, e.g. '+14155552671'"
            )
        return value


class UserUpdate(UserProfileFields):
    """Partial profile update; every field optional so PATCH may send a subset.

    Email is deliberately excluded: changing a login identifier needs
    verification of the new address, which is a flow, not a field.

    ``extra="forbid"`` means a client typo (``displayName``) is a 422 rather
    than a silently ignored write the user believes succeeded.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, max_length=FULL_NAME_MAX_LENGTH)


class UserRead(UserBase):
    """A user as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    is_active: bool
    created_at: datetime

    # Timestamps rather than booleans: the client can render "verified" from a
    # non-null value, and support can answer "when".
    email_verified_at: datetime | None = None
    onboarding_completed_at: datetime | None = None

    display_name: str | None = None
    bio: str | None = None
    avatar_url: str | None = None
    timezone: str | None = None
    locale: str | None = None
    phone_number: str | None = None


class DeleteAccountRequest(BaseModel):
    """Payload for deleting your own account.

    The password is re-checked even though the caller is already authenticated:
    it proves a live human holds the credential, so a stolen access token alone
    cannot destroy the account.
    """

    model_config = ConfigDict(extra="forbid")

    password: str = Field(max_length=PASSWORD_MAX_LENGTH)
