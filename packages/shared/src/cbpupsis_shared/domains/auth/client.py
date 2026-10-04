"""Internal client for the auth domain — the extraction swap point.

Other domains depend on THIS module, never on ``service.py`` directly. Today it
forwards to the local service; if auth is ever extracted, only this file changes.

The functions exposed here are deliberately few. Auth's public surface to other
domains is not "the login flow" — it is the two things another domain needs when
it changes an account: *end this user's sessions*, and *check this password*.
Everything else stays behind the HTTP endpoints.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.auth import service
from cbpupsis_shared.domains.auth.security import (
    decode_token,
    require_session_version,
    verify_password,
)


async def revoke_all_sessions(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Revoke every live refresh token for a user.

    Called by the users domain when an account is deactivated or deleted: the
    users table owns ``is_active``, but the token ledger belongs to auth, so
    cutting sessions has to cross the boundary here.
    """
    await service.revoke_all_for_user(db, user_id)


async def stage_revoke_all_sessions(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Stage session revocation for a transaction owned by another domain."""
    await service.stage_revoke_all_for_user(db, user_id)


def check_password(raw_password: str, password_hash: str) -> bool:
    """Return whether a raw password matches a stored hash.

    Password hashing is auth's concern, so the comparison is exposed rather than
    letting another domain import ``passlib`` and pick its own scheme — two
    hashing configurations in one codebase is how one of them ends up weak.
    """
    return verify_password(raw_password, password_hash)


def decode_access_session(token: str) -> dict[str, object]:
    """Validate an access token and expose its typed session claims."""
    return decode_token(token, expected_type="access")


def decode_refresh_session(token: str) -> dict[str, object]:
    """Validate a refresh token before a role-specific rotation begins."""
    return decode_token(token, expected_type="refresh")


def validate_session_version(claims: dict[str, object], current_version: int) -> None:
    """Apply account-wide access revocation to every role's session guard."""
    require_session_version(claims, current_version)
