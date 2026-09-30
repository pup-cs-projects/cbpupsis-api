"""Persistence owned by the Admin authentication domain."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from cbpupsis_database.base import Base, UUIDMixin
from cbpupsis_database.models.auth import UserMfaCredential as MfaCredential
from cbpupsis_database.models.users import AdminProfile


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


class SuperadminChallenge(Base):
    """The sole live password-step challenge for one Superadmin."""

    __tablename__ = "superadmin_challenges"

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    active_jti: Mapped[str] = mapped_column(String(36))


__all__ = [
    "AdminPosition",
    "AdminProfile",
    "AuthFailureStep",
    "AuthenticationFailure",
    "AuthenticationLockout",
    "MfaCredential",
    "MfaType",
    "SuperadminChallenge",
]
