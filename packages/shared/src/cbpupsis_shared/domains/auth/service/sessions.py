"""Server-side sessions and the account gates every session passes.

Every session has a ``sid`` row in ``user_active_sessions``, which is what lets
:func:`check_session` end it after the idle timeout for every role.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_shared.domains.auth import repository
from cbpupsis_shared.domains.auth.constants import ROLE_SUPERADMIN
from cbpupsis_shared.domains.auth.exceptions import (
    AuthSessionExpiredError,
    InactiveUserError,
    InvalidAuthTokenError,
    PasswordChangeRequiredError,
    SuperadminRoleRequiredError,
    UnverifiedEmailError,
)
from cbpupsis_shared.domains.auth.security import require_session_version
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP, SUPERADMIN_GROUP
from cbpupsis_shared.domains.users import service as users_service


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def require_verified_email(user) -> None:
    """Raise if ``user`` has not verified their email address.

    Shared by login and refresh so the rule has exactly one definition: a second
    copy in the refresh path is how the two drift and refresh quietly keeps
    minting access tokens for accounts login has stopped admitting.
    """
    if user.email_verified_at is None:
        raise UnverifiedEmailError(
            "Verify your email address to continue. "
            "Check your inbox, or request a new verification link."
        )


async def require_superadmin_actor(db: AsyncSession, user_id: uuid.UUID) -> None:
    user = await users_service.get_active_user(db, user_id)
    if (
        user is None
        or user.email_verified_at is None
        or not await iam_service.is_user_in_group(db, user_id, SUPERADMIN_GROUP)
        or await iam_service.is_user_in_group(db, user_id, ADMIN_GROUP)
    ):
        raise SuperadminRoleRequiredError


def session_ids_from_claims(claims: dict) -> tuple[uuid.UUID, uuid.UUID]:
    """Return the token's ``(user_id, session_id)``; a malformed claim is invalid."""
    try:
        return uuid.UUID(str(claims["sub"])), uuid.UUID(str(claims["sid"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidAuthTokenError from exc


async def check_session(
    db: AsyncSession,
    claims: dict,
    *,
    touch: bool = True,
    allow_password_change: bool = False,
) -> uuid.UUID:
    """Require the token's server-side session to be live, for every role.

    A session idle for ``session_idle_timeout_minutes`` is deleted, together
    with the refresh tokens bound to it, and the request answers
    ``AUTH_SESSION_EXPIRED``. A token without a session id is refused.

    A live session of an account still holding a temporary password answers
    ``PASSWORD_CHANGE_REQUIRED``, unless the caller is the password change
    itself or a flow that only maintains the session (``allow_password_change``).
    Every role's session guard calls this, so the rule has one home.
    """
    user_id, sid = session_ids_from_claims(claims)
    user = await users_service.get_active_user_for_update(db, user_id)
    if user is None:
        raise InactiveUserError
    require_session_version(claims, user.session_version)
    session = await repository.get_active_session(db, user_id, sid)
    now = datetime.now(UTC)
    idle_limit = now - timedelta(minutes=settings.session_idle_timeout_minutes)
    if session is None or as_utc(session.last_activity_at) <= idle_limit:
        await repository.delete_active_sessions(db, user_id, sid)
        await db.commit()
        raise AuthSessionExpiredError
    change_required = user.password_change_required
    if touch:
        repository.touch_active_session(session, now)
    await db.commit()
    if change_required and not allow_password_change:
        raise PasswordChangeRequiredError
    return sid


async def check_superadmin_session(
    db: AsyncSession,
    claims: dict,
    *,
    touch: bool = True,
    allow_password_change: bool = False,
) -> uuid.UUID:
    """:func:`check_session` plus the live Superadmin role and MFA claim."""
    if claims.get("role") != ROLE_SUPERADMIN or claims.get("mfa") is not True:
        raise SuperadminRoleRequiredError
    user_id, _sid = session_ids_from_claims(claims)
    try:
        await require_superadmin_actor(db, user_id)
    except SuperadminRoleRequiredError:
        await repository.delete_active_sessions(db, user_id)
        await db.commit()
        raise
    return await check_session(
        db, claims, touch=touch, allow_password_change=allow_password_change
    )


def stage_session(
    db: AsyncSession, *, user_id: uuid.UUID, ip_address: str | None, device_info: str
) -> uuid.UUID:
    """Stage a server-side session row and return its id (the ``sid`` claim)."""
    sid = uuid.uuid4()
    repository.add_active_session(
        db,
        id=sid,
        user_id=user_id,
        session_token_hash=hashlib.sha256(sid.bytes).hexdigest(),
        ip_address=ip_address,
        device_info=device_info[:512],
        is_current=True,
        last_activity_at=datetime.now(UTC),
    )
    return sid
