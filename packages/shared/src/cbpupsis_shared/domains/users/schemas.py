"""Pydantic schemas (DTOs) for the users domain.

The API contract, split by direction: Base holds shared fields, Read adds
server-owned ones, Update carries partial edits. ``AccountCreate`` is one
Superadmin-created account: the JSON body of ``POST /users`` and one row of the
CSV import. It carries no password; the system generates a temporary one.

Note what is absent: ``password_hash`` appears in no schema, so it cannot leak
through a response by accident.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from cbpupsis_shared.domains.auth.constants import (
    INSTITUTIONAL_ID_PATTERN,
    PASSWORD_MAX_LENGTH,
)
from cbpupsis_shared.domains.users.constants import (
    AVATAR_URL_MAX_LENGTH,
    COLLEGE_MAX_LENGTH,
    DEPARTMENT_MAX_LENGTH,
    DISPLAY_NAME_MAX_LENGTH,
    E164_PATTERN,
    FULL_NAME_MAX_LENGTH,
    INSTITUTIONAL_ID_MAX_LENGTH,
    LOCALE_MAX_LENGTH,
    LOCALE_PATTERN,
    PHONE_NUMBER_MAX_LENGTH,
    PROFILE_NAME_MAX_LENGTH,
    ROLE_REQUIRED_FIELDS,
    TIMEZONE_MAX_LENGTH,
    YEAR_LEVEL_MAX,
    YEAR_LEVEL_MIN,
    AccountRole,
)


class UserBase(BaseModel):
    """Fields shared by user input and output."""

    email: EmailStr
    full_name: str | None = Field(default=None, max_length=FULL_NAME_MAX_LENGTH)


class RecordIdentity(BaseModel):
    student_number: str
    full_name: str


class ScheduleIdentity(BaseModel):
    """Student identity for a private, caller-owned schedule."""

    student_number: str
    full_name: str


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
    def check_avatar_url(cls, value: str | None) -> str | None:
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
    def check_timezone(cls, value: str | None) -> str | None:
        """Reject anything the standard library does not know as an IANA zone.

        A free-text timezone is a bug waiting for the first render: storing
        "PST" or "GMT+2" produces a value nothing can convert with.
        """
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"{value!r} is not a valid IANA timezone name") from exc
        return value

    @field_validator("locale")
    @classmethod
    def check_locale(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not LOCALE_PATTERN.match(value):
            raise ValueError("locale must be a BCP 47 tag, e.g. 'en' or 'en-US'")
        return value

    @field_validator("phone_number")
    @classmethod
    def check_phone_number(cls, value: str | None) -> str | None:
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


class AdminProfileRead(BaseModel):
    """An Admin's position and data-reach scope, for the admin sign-in domain."""

    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    position: str
    department: str | None = None
    college: str | None = None
    is_active: bool


class FacultyProfileRead(BaseModel):
    """A faculty member's employment identity, for the faculty sign-in domain."""

    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    employee_id: str | None = None
    department: str
    college: str | None = None
    is_active: bool


class AccountCreate(BaseModel):
    """One account a Superadmin creates: a ``POST /users`` body or a CSV row.

    Which optional fields are required depends on ``role``
    (``ROLE_REQUIRED_FIELDS``). Error messages never repeat the submitted value,
    because the CSV import reports them back with only a row and a column.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    first_name: str = Field(min_length=1, max_length=PROFILE_NAME_MAX_LENGTH)
    surname: str = Field(min_length=1, max_length=PROFILE_NAME_MAX_LENGTH)
    email: EmailStr
    role: AccountRole
    institutional_id: str | None = Field(
        default=None, max_length=INSTITUTIONAL_ID_MAX_LENGTH
    )
    birthdate: date | None = None
    department: str | None = Field(default=None, max_length=DEPARTMENT_MAX_LENGTH)
    college: str | None = Field(default=None, max_length=COLLEGE_MAX_LENGTH)
    #: A program code, matched case-insensitively against active programs.
    program: str | None = None
    year_level: int | None = Field(default=None, ge=YEAR_LEVEL_MIN, le=YEAR_LEVEL_MAX)

    @field_validator(
        "institutional_id", "department", "college", "program", mode="before"
    )
    @classmethod
    def blank_is_absent(cls, value: object) -> object:
        """An empty cell or string means the field was not supplied."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("email")
    @classmethod
    def lower_email(cls, value: str) -> str:
        return value.lower()

    @field_validator("institutional_id")
    @classmethod
    def check_institutional_id(cls, value: str | None) -> str | None:
        """Student numbers and employee ids share one shape (BR-AUTH-001)."""
        if value is not None and not INSTITUTIONAL_ID_PATTERN.match(value):
            raise PydanticCustomError(
                "institutional_id_format", "must match YYYY-NNNNN-XX-N"
            )
        return value

    @field_validator("birthdate")
    @classmethod
    def check_birthdate(cls, value: date | None) -> date | None:
        if value is not None and value >= datetime.now(UTC).date():
            raise PydanticCustomError("birthdate_not_past", "must be in the past")
        return value

    @model_validator(mode="after")
    def check_role_fields(self) -> AccountCreate:
        missing = [
            name
            for name in ROLE_REQUIRED_FIELDS[self.role]
            if getattr(self, name) is None
        ]
        if missing:
            raise PydanticCustomError(
                "role_fields_missing",
                "required for this role: {fields}",
                {"fields": ", ".join(missing)},
            )
        return self
