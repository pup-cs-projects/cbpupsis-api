"""The sign-in flow every role shares: password step, MFA challenge, session.

Each role domain passes only what differs: the role, how its profile is loaded
and checked, why a credential step failed, and its response schema. Factors
that belong to one role (the Admin WebAuthn ceremony) stay in that role's app,
so the student and faculty images never import them.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_database.models.auth import AuthFailureStep
from cbpupsis_shared.domains.audit import client as audit_client
from cbpupsis_shared.domains.auth.exceptions import (
    AccountLockedError,
    AuthError,
    InactiveUserError,
    InvalidAuthTokenError,
    TemporaryPasswordExpiredError,
)
from cbpupsis_shared.domains.auth.schemas import Challenge, TokenPair
from cbpupsis_shared.domains.auth.security import (
    decode_challenge_token,
    lockout_subject,
    require_session_version,
)
from cbpupsis_shared.domains.auth.service.lockout import (
    clear_sign_in_failures,
    record_sign_in_failure,
    require_not_locked,
)
from cbpupsis_shared.domains.auth.service.mfa import (
    require_bound_challenge,
    retire_mfa_challenge,
)
from cbpupsis_shared.domains.auth.service.passwords import check_password
from cbpupsis_shared.domains.auth.service.sessions import as_utc
from cbpupsis_shared.domains.auth.service.tokens import issue_role_session
from cbpupsis_shared.domains.users import service as users_service


def record_attempt(
    db: AsyncSession,
    *,
    attempted_id: str,
    user_id: uuid.UUID | None,
    ip_address: str | None,
    device_info: str,
    failure_reason: str | None = None,
    mfa_used: bool = False,
) -> None:
    """Stage one row on the sign-in ledger; success when there is no reason."""
    audit_client.stage_sign_in_attempt(
        db,
        attempted_id=attempted_id,
        user_id=user_id,
        ip_address=ip_address,
        user_agent=device_info,
        success=failure_reason is None,
        failure_reason=failure_reason,
        mfa_used=mfa_used,
    )


def temporary_password_expired(
    password_change_required: bool, expires_at: datetime | None
) -> bool:
    """Whether a still-unreplaced temporary password is past its expiry."""
    return (
        password_change_required
        and expires_at is not None
        and as_utc(expires_at) <= datetime.now(UTC)
    )


async def refuse_expired_temporary_password(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    password_change_required: bool,
    expires_at: datetime | None,
    attempted_id: str,
    ip_address: str | None,
    device_info: str,
) -> None:
    """Refuse a sign-in whose correct password is an expired temporary one.

    Every sign-in calls this once its credential step has passed, so the
    answer only reaches a caller who already holds the password. Recorded on
    the ledger and committed before raising; it is not a failed credential, so
    it does not count toward the lockout. A Superadmin must reissue.
    """
    if not temporary_password_expired(password_change_required, expires_at):
        return
    record_attempt(
        db,
        attempted_id=attempted_id,
        user_id=user_id,
        ip_address=ip_address,
        device_info=device_info,
        failure_reason="temporary_password_expired",
    )
    await db.commit()
    raise TemporaryPasswordExpiredError


async def verify_identifier_password(
    db: AsyncSession,
    *,
    identifier: str,
    subject,
    password: str,
    ip_address: str | None,
    device_info: str,
    reject: Callable[[uuid.UUID, bool], Awaitable[str | None]],
    failure: AuthError,
) -> uuid.UUID:
    """Check an institutional identifier's password under the shared lockout.

    ``subject`` is whoever holds ``identifier`` (``None`` if nobody). An unknown
    identifier locks through its keyed pseudonym exactly like an account, and
    its password is still hashed, so neither the answer nor its timing reveals
    which identifiers exist. ``reject`` names the role's failed check for the
    ledger, or returns ``None``; the caller only ever sees ``failure``.
    Returns the subject's user id. A success is not recorded here: the role
    decides whether this step or the session is the completed sign-in.
    """
    subject_id = subject.user_id if subject is not None else lockout_subject(identifier)
    user_id = subject.user_id if subject is not None else None
    try:
        await require_not_locked(db, subject_id)
    except AccountLockedError:
        record_attempt(
            db,
            attempted_id=identifier,
            user_id=user_id,
            ip_address=ip_address,
            device_info=device_info,
            failure_reason="account_locked",
        )
        await db.commit()
        raise

    password_ok = await check_password(
        password, subject.password_hash if subject is not None else None
    )
    reason = await reject(subject_id, password_ok)
    if reason is not None:
        record_attempt(
            db,
            attempted_id=identifier,
            user_id=user_id,
            ip_address=ip_address,
            device_info=device_info,
            failure_reason=reason,
        )
        await record_sign_in_failure(
            db, subject_id, AuthFailureStep.credentials, notify=subject is not None
        )
        raise failure
    await refuse_expired_temporary_password(
        db,
        user_id=subject.user_id,
        password_change_required=subject.password_change_required,
        expires_at=subject.temporary_password_expires_at,
        attempted_id=identifier,
        ip_address=ip_address,
        device_info=device_info,
    )
    return subject_id


async def open_challenge[P](
    db: AsyncSession,
    token: str,
    *,
    role: str,
    load_profile: Callable[[AsyncSession, uuid.UUID], Awaitable[P]],
) -> Challenge[P]:
    """Accept a role's MFA challenge token only while everything behind it holds.

    Locks the account row (shared with resets), then requires: no lockout, the
    token's ``session_version``, the role's live profile (``load_profile``
    raises the role's own error), and the token being the latest one issued.
    """
    claims = decode_challenge_token(token, role=role)
    try:
        user_id = uuid.UUID(str(claims["sub"]))
    except (KeyError, ValueError) as exc:
        raise InvalidAuthTokenError from exc
    user = await users_service.get_active_user_for_update(db, user_id)
    if user is None:
        raise InactiveUserError
    await require_not_locked(db, user_id)
    require_session_version(claims, user.session_version)
    profile = await load_profile(db, user_id)
    await require_bound_challenge(db, user_id, claims.get("jti"))
    return Challenge(claims=claims, user_id=user_id, profile=profile)


async def complete_sign_in(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    role: str,
    attempted_id: str,
    ip_address: str | None,
    device_info: str,
    mfa: bool,
    claims: dict[str, object] | None = None,
    commit: bool = True,
) -> TokenPair:
    """Record the completed sign-in, clear its lockout state, open the session."""
    record_attempt(
        db,
        attempted_id=attempted_id,
        user_id=user_id,
        ip_address=ip_address,
        device_info=device_info,
        mfa_used=mfa,
    )
    if mfa:
        await retire_mfa_challenge(db, user_id)
    await clear_sign_in_failures(db, user_id)
    return await issue_role_session(
        db,
        user_id,
        role=role,
        mfa=mfa,
        claims=claims,
        ip_address=ip_address,
        device_info=device_info,
        commit=commit,
    )


def expiring_pair[T: TokenPair](schema: type[T], pair: TokenPair) -> T:
    """Wrap a pair in a role schema that also states the access token's lifetime."""
    return schema(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=settings.access_token_ttl_minutes * 60,
    )
