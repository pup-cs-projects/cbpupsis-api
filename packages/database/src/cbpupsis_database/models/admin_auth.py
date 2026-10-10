"""Persistence owned by the Admin authentication domain."""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from cbpupsis_database.base import Base
from cbpupsis_database.models.auth import (
    AuthenticationFailure,
    AuthenticationLockout,
    AuthFailureStep,
)
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
