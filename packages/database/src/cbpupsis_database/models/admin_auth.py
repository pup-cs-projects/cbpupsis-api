"""Persistence owned by the Admin authentication domain."""

from __future__ import annotations

import enum

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


__all__ = [
    "AdminPosition",
    "AdminProfile",
    "AuthFailureStep",
    "AuthenticationFailure",
    "AuthenticationLockout",
    "MfaCredential",
    "MfaType",
]
