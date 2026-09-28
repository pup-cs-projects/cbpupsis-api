"""SQLAlchemy models for the users domain.

This table owns identity and profile data, including the password hash. The raw
password is never stored — only an argon2 digest produced by
``cbpupsis_shared.domains.auth.security`` — and no read schema exposes even the hash.

Only this domain reads or writes this table; other domains go through
``cbpupsis_shared.domains.users.service`` (or ``client.py``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from cbpupsis_database.base import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin

EMAIL_MAX_LENGTH = 320
FULL_NAME_MAX_LENGTH = 255
DISPLAY_NAME_MAX_LENGTH = 100
AVATAR_URL_MAX_LENGTH = 2048
TIMEZONE_MAX_LENGTH = 64
LOCALE_MAX_LENGTH = 35
PHONE_NUMBER_MAX_LENGTH = 20


class User(UUIDMixin, TimestampMixin, SoftDeleteMixin, Base):
    """An application user.

    Inherits a UUID primary key, audit timestamps, and soft-delete support from
    the shared mixins.
    """

    __tablename__ = "users"

    #: Login identifier. Stored lower-cased so uniqueness is case-insensitive;
    #: without that, Foo@x.com and foo@x.com would be two accounts.
    email: Mapped[str] = mapped_column(
        String(EMAIL_MAX_LENGTH), unique=True, index=True
    )
    #: Argon2 digest. Sized for argon2 output plus room for future parameters.
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(
        String(FULL_NAME_MAX_LENGTH), default=None
    )
    #: Cleared to suspend an account without deleting it. Checked at login and
    #: on every authenticated request.
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )

    # --- Email verification ---
    #: When the address was proven reachable; NULL means unverified. A timestamp
    #: rather than a boolean because "when" is the question asked in support and
    #: audit, and it is free to store.
    #:
    #: Unverified users can still log in — see the module docstring of
    #: ``cbpupsis_shared.domains.auth.dependencies`` for why, and use
    #: ``require_verified_email`` to gate the endpoints that genuinely need it.
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    # --- Profile / onboarding ---
    #: Public-facing name, distinct from ``full_name`` (the legal/full one) so a
    #: user can be shown as "jp" without losing the name on file.
    display_name: Mapped[str | None] = mapped_column(
        String(DISPLAY_NAME_MAX_LENGTH), default=None
    )
    #: Free text; Text rather than String because a length cap on a bio is an
    #: arbitrary limit that will be raised later. Bounded at the schema layer.
    bio: Mapped[str | None] = mapped_column(Text, default=None)
    #: URL of an externally hosted image. This application stores no files: an
    #: upload pipeline is a separate concern (presigned URLs, virus scanning,
    #: content-type checks) that does not belong in the users table.
    avatar_url: Mapped[str | None] = mapped_column(
        String(AVATAR_URL_MAX_LENGTH), default=None
    )
    #: IANA zone name, e.g. "Europe/Lisbon". Used to render stored-UTC instants.
    timezone: Mapped[str | None] = mapped_column(
        String(TIMEZONE_MAX_LENGTH), default=None
    )
    #: BCP 47 language tag, e.g. "en-US".
    locale: Mapped[str | None] = mapped_column(String(LOCALE_MAX_LENGTH), default=None)
    #: E.164 format. Not unique and not a login identifier — making it either
    #: needs an ownership-proving SMS flow this template does not have.
    phone_number: Mapped[str | None] = mapped_column(
        String(PHONE_NUMBER_MAX_LENGTH), default=None
    )
    #: Stamped when the user supplies the required profile fields; NULL means
    #: the client should still route them into onboarding.
    onboarding_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    #: Set when the row's personal data was scrubbed by an account deletion.
    #: Distinct from ``deleted_at``: soft deletion says the row is gone from the
    #: application's point of view, this says the PII in it is genuinely erased.
    #: Keeping them separate is what lets a deletion be audited ("erased on
    #: 2026-08-28") without retaining anything that identifies the person.
    anonymized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
