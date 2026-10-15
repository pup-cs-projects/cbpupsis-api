"""SQLAlchemy models for the users domain.

This table owns identity and profile data, including the password hash. The raw
password is never stored — only a bcrypt digest produced by
``cbpupsis_shared.domains.auth.security`` — and no read schema exposes even the hash.

Only this domain reads or writes this table; other domains go through
``cbpupsis_shared.domains.users.service`` (or ``client.py``).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
)
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
    #: Password digest. Existing Argon2 values remain valid until changed.
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(
        String(FULL_NAME_MAX_LENGTH), default=None
    )
    #: Cleared to suspend an account without deleting it. Checked at login and
    #: on every authenticated request.
    session_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
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


class UserProfile(Base):
    """Personal details, demographics, and contact addresses."""

    __tablename__ = "user_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    first_name: Mapped[str] = mapped_column(String(100))
    middle_name: Mapped[str | None] = mapped_column(String(100), default=None)
    last_name: Mapped[str] = mapped_column(String(100))
    birthdate: Mapped[date | None] = mapped_column(Date, default=None)
    gender: Mapped[str | None] = mapped_column(String(20), default=None)
    civil_status: Mapped[str | None] = mapped_column(String(30), default=None)
    personal_email: Mapped[str | None] = mapped_column(String(255), default=None)
    institutional_email: Mapped[str | None] = mapped_column(String(255), default=None)
    phone_number: Mapped[str | None] = mapped_column(String(25), default=None)
    avatar_url: Mapped[str | None] = mapped_column(Text, default=None)
    home_address: Mapped[str | None] = mapped_column(Text, default=None)
    current_address: Mapped[str | None] = mapped_column(Text, default=None)
    profile_completed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    email_notifications: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AdminProfile(Base):
    """Administrative staff profile and assigned department/college."""

    __tablename__ = "admin_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    employee_id: Mapped[str | None] = mapped_column(
        String(30), unique=True, default=None
    )
    position: Mapped[str] = mapped_column(String(50))
    department: Mapped[str | None] = mapped_column(String(100), default=None)
    college: Mapped[str | None] = mapped_column(String(100), default=None)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    active_mfa_challenge_jti: Mapped[str | None] = mapped_column(
        String(36), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FacultyProfile(Base):
    """Faculty instructor employment details and rank."""

    __tablename__ = "faculty_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    employee_id: Mapped[str | None] = mapped_column(
        String(30), unique=True, default=None
    )
    college: Mapped[str | None] = mapped_column(String(100), default=None)
    department: Mapped[str] = mapped_column(String(100))
    academic_rank: Mapped[str | None] = mapped_column(String(50), default=None)
    employment_type: Mapped[str] = mapped_column(
        String(30), default="full_time", server_default="full_time"
    )
    office_location: Mapped[str | None] = mapped_column(String(100), default=None)
    office_hours: Mapped[str | None] = mapped_column(String(255), default=None)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )


class StudentProfile(Base):
    """Student profile, program attachment, and academic holds."""

    __tablename__ = "student_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    student_number: Mapped[str] = mapped_column(String(30), unique=True)
    program_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("programs.id", ondelete="RESTRICT")
    )
    curriculum_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    year_level: Mapped[int] = mapped_column(SmallInteger)
    section: Mapped[str | None] = mapped_column(String(20), default=None)
    enrollment_status: Mapped[str] = mapped_column(
        String(30), default="pending", server_default="pending"
    )
    fhe_qualified: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    fhe_forfeited: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    fhe_forfeiture_reason: Mapped[str | None] = mapped_column(String(255), default=None)
    is_graduating_senior: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    has_financial_hold: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    has_academic_hold: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["curriculum_id", "program_id"],
            ["curricula.id", "curricula.program_id"],
            ondelete="SET NULL",
        ),
        Index("idx_fk_student_profiles_program_id", "program_id"),
        Index("ix_student_profiles_student_number", "student_number", unique=True),
    )


class EmergencyContact(UUIDMixin, Base):
    """Emergency contacts for students/staff."""

    __tablename__ = "emergency_contacts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    contact_name: Mapped[str] = mapped_column(String(150))
    relationship: Mapped[str] = mapped_column(String(50))
    phone_number: Mapped[str] = mapped_column(String(25))
    email: Mapped[str | None] = mapped_column(String(255), default=None)
    address: Mapped[str | None] = mapped_column(Text, default=None)
    is_primary: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )

    __table_args__ = (Index("idx_fk_emergency_contacts_user_id", "user_id"),)
