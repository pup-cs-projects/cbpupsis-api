"""Persistence owned by the Admin authentication domain."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from cbpupsis_database.base import Base, UUIDMixin


class AdminPosition(enum.StrEnum):
    """The data-reach position carried by the single Admin role."""

    chairperson = "chairperson"
    dean = "dean"
    registrar = "registrar"


class MfaType(enum.StrEnum):
    """Supported Admin second-factor mechanisms."""

    totp = "totp"
    webauthn = "webauthn"


class AuthFailureStep(enum.StrEnum):
    """Both halves of Admin sign-in share one failure window."""

    credentials = "credentials"
    mfa = "mfa"


class AdminProfile(Base):
    """An Admin's position and query scope from the central persona schema."""

    __tablename__ = "admin_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    employee_id: Mapped[str | None] = mapped_column(String(30), unique=True)
    position: Mapped[str] = mapped_column(String(50))
    department: Mapped[str | None] = mapped_column(String(100))
    college: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    active_mfa_challenge_jti: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MfaCredential(UUIDMixin, Base):
    """One encrypted TOTP seed or WebAuthn credential for an Admin."""

    __tablename__ = "user_mfa_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column()
    mfa_type: Mapped[str] = mapped_column(String(20))
    totp_secret: Mapped[str | None] = mapped_column(String(255))
    credential_id: Mapped[str | None] = mapped_column(Text)
    credential_id_digest: Mapped[str | None] = mapped_column(String(64))
    public_key: Mapped[str | None] = mapped_column(Text)
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    is_enrolled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("idx_fk_user_mfa_credentials_user_id", "user_id"),
        Index(
            "ix_user_mfa_credential_id_digest",
            "credential_id_digest",
            unique=True,
            postgresql_where=credential_id_digest.is_not(None),
        ),
    )


class AuthenticationFailure(UUIDMixin, Base):
    """One failed password or MFA attempt in the rolling lockout window."""

    __tablename__ = "authentication_failures"

    user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    step: Mapped[AuthFailureStep] = mapped_column(
        Enum(
            AuthFailureStep,
            native_enum=False,
            length=32,
            create_constraint=True,
            name="ck_authentication_failures_step",
        )
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class AuthenticationLockout(Base):
    """The current temporary Admin account lock, if any."""

    __tablename__ = "authentication_lockouts"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    locked_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
