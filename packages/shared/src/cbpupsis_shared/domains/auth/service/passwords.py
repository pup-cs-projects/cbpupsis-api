"""Password checks, recovery (unauthenticated), and change (authenticated)."""

from __future__ import annotations

import asyncio
import math
import uuid
from datetime import UTC, datetime, timedelta

from pydantic import EmailStr, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_core.events import Event
from cbpupsis_database.models.auth import TokenPurpose
from cbpupsis_shared.domains.audit import client as audit_client
from cbpupsis_shared.domains.audit import service as audit_service
from cbpupsis_shared.domains.auth import repository
from cbpupsis_shared.domains.auth.constants import ROLE_SUPERADMIN
from cbpupsis_shared.domains.auth.exceptions import (
    AuthError,
    AuthSessionExpiredError,
    InactiveUserError,
    IncorrectCurrentPasswordError,
    InvalidEmailAddressError,
    InvalidTokenError,
    PasswordTooWeakError,
    ResetRateLimitError,
    ResetTokenExpiredError,
    ResetTokenUsedError,
    TemporaryPasswordExpiredError,
)
from cbpupsis_shared.domains.auth.schemas import TokenPair
from cbpupsis_shared.domains.auth.security import (
    DUMMY_PASSWORD_HASH,
    RESET_EMAIL,
    check_password_complexity,
    encrypt_mailed_token,
    generate_one_time_token,
    hash_one_time_token,
    hash_password,
    reset_address_hash,
    verify_password,
)
from cbpupsis_shared.domains.auth.service.sessions import as_utc, stage_session
from cbpupsis_shared.domains.auth.service.tokens import (
    carried_claims,
    issue_pair,
    stage_revoke_all_for_user,
)
from cbpupsis_shared.domains.auth.service.verification import invalidate_outstanding
from cbpupsis_shared.domains.users import service as users_service
from cbpupsis_shared.outbox import publish_transactional


async def check_password(raw_password: str, password_hash: str | None) -> bool:
    """Verify a password off the event loop, at equal cost for unknown accounts.

    A missing account is checked against a dummy digest so it takes as long as
    a wrong password, and response time cannot reveal which accounts exist.
    """
    if password_hash is None:
        await asyncio.to_thread(verify_password, raw_password, DUMMY_PASSWORD_HASH)
        return False
    return await asyncio.to_thread(verify_password, raw_password, password_hash)


async def forgot_password(
    db: AsyncSession, email: str, *, ip_address: str | None = None
) -> None:
    """Atomically issue a digest, encrypted delivery job, and one audit row."""
    try:
        email = str(TypeAdapter(EmailStr).validate_python(email)).lower()
    except ValueError:
        audit_service.stage_record(
            db,
            action="auth.password_reset_requested",
            actor_id=None,
            ip_address=ip_address,
            outcome="INVALID_EMAIL_ADDRESS",
        )
        await db.commit()
        raise InvalidEmailAddressError from None
    now = datetime.now(UTC)
    email_hash = reset_address_hash(email)
    window = timedelta(seconds=settings.password_reset_request_window_seconds)
    allowed = not settings.rate_limit_enabled or await repository.claim_reset_request(
        db, email_hash, now, now - window, settings.password_reset_request_limit
    )
    user = await users_service.get_by_email_for_update(db, email.lower())
    actor_id = user.id if user is not None else None
    if not allowed:
        started = await repository.reset_request_retry_after(db, email_hash)
        retry_after = max(
            1, math.ceil((as_utc(started) + window - now).total_seconds())
        )
        audit_service.stage_record(
            db,
            action="auth.password_reset_requested",
            actor_id=actor_id,
            ip_address=ip_address,
            outcome="rate_limited",
        )
        await db.commit()
        raise ResetRateLimitError(retry_after)

    if user is not None and user.is_active:
        raw_token, token_hash = generate_one_time_token()
        # Keep previous links usable: anonymous requests must not cancel recovery.
        repository.add_one_time_token(
            db,
            user_id=user.id,
            token_hash=token_hash,
            purpose=TokenPurpose.password_reset,
            expires_at=now + timedelta(minutes=settings.password_reset_ttl_minutes),
        )
        publish_transactional(
            db,
            Event(
                name="auth.password_reset_email",
                payload={
                    "user_id": str(user.id),
                    "token_hash": token_hash,
                    "encrypted_token": encrypt_mailed_token(
                        raw_token,
                        label=RESET_EMAIL,
                        user_id=user.id,
                        token_hash=token_hash,
                    ),
                },
            ),
        )
    audit_service.stage_record(
        db,
        action="auth.password_reset_requested",
        actor_id=actor_id,
        ip_address=ip_address,
        outcome="accepted",
    )
    await db.commit()


async def verify_reset_token(db: AsyncSession, raw_token: str):
    """Check a link without spending it; safe for form loading and mail previews."""
    record = await repository.get_one_time_token(
        db, hash_one_time_token(raw_token), TokenPurpose.password_reset
    )
    if record is None:
        raise InvalidTokenError
    if record.consumed_at is not None:
        raise ResetTokenUsedError
    if as_utc(record.expires_at) <= datetime.now(UTC):
        raise ResetTokenExpiredError
    if await users_service.get_active_user(db, record.user_id) is None:
        raise InvalidTokenError
    return record


async def reset_password(
    db: AsyncSession,
    raw_token: str,
    new_password: str,
    *,
    ip_address: str | None = None,
) -> int:
    """Consume the token and close every session in one account-locked transaction."""
    actor_id = None
    try:
        record = await repository.get_one_time_token(
            db, hash_one_time_token(raw_token), TokenPurpose.password_reset
        )
        if record is not None:
            actor_id = record.user_id
        await verify_reset_token(db, raw_token)
        unmet = check_password_complexity(new_password)
        if unmet:
            raise PasswordTooWeakError(unmet)
        digest = await asyncio.to_thread(hash_password, new_password)
        user = await users_service.get_active_user_for_update(db, actor_id)
        if user is None:
            raise InvalidTokenError
        now = datetime.now(UTC)
        user_id = await repository.consume_one_time_token(
            db, hash_one_time_token(raw_token), TokenPurpose.password_reset, now
        )
        if user_id is None:
            await verify_reset_token(db, raw_token)
            raise ResetTokenUsedError
        await users_service.stage_password_reset(db, user_id, digest)
        sessions_revoked = await stage_revoke_all_for_user(db, user_id)
        await repository.delete_authentication_lockout(db, user_id)
        await repository.delete_authentication_failures(db, user_id)
        await repository.delete_mfa_challenge(db, user_id)
        await invalidate_outstanding(db, user_id, TokenPurpose.password_reset)
        stage_password_changed_notice(db, user_id)
        audit_service.stage_record(
            db,
            action="auth.password_reset_completed",
            actor_id=user_id,
            ip_address=ip_address,
            outcome="success",
            sessions_revoked=sessions_revoked,
        )
        await db.commit()
        return sessions_revoked
    except AuthError as exc:
        await db.rollback()
        audit_service.stage_record(
            db,
            action="auth.password_reset_completed",
            actor_id=actor_id,
            ip_address=ip_address,
            outcome=exc.code or "invalid_token",
        )
        await db.commit()
        raise
    except Exception:
        await db.rollback()
        audit_service.stage_record(
            db,
            action="auth.password_reset_completed",
            actor_id=actor_id,
            ip_address=ip_address,
            outcome="failed",
        )
        await db.commit()
        raise


def stage_password_changed_notice(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Stage the password-changed notice for durable delivery.

    Staged, not sent: the row commits with the caller's transaction, so the
    notice cannot be lost between "the password changed" and "the owner was
    told", which is the one window where losing it matters most.
    """
    publish_transactional(
        db,
        Event(name="auth.password_changed", payload={"user_id": str(user_id)}),
    )


async def change_password(
    db: AsyncSession,
    user_id: uuid.UUID,
    current_password: str,
    new_password: str,
    session_id: uuid.UUID,
    session_claims: dict[str, object],
) -> TokenPair:
    """Change an authenticated user's password, returning a fresh token pair.

    The new pair keeps the calling session's role claims (role, MFA, Admin
    scope) and its address, so changing a password never downgrades or
    upgrades the session that asked for it.

    Requires the current password: an access token alone must not be enough to
    take permanent ownership of an account.

    This is the one route open while a temporary password awaits replacement.
    The temporary password cannot be kept as the new one, and once it has
    expired it cannot be changed either: a Superadmin must reissue.

    **All** refresh tokens are revoked, including the caller's, and a new pair
    is returned. The alternative (keeping the calling session alive) needs the
    caller's refresh token in the request body to know which one to spare, which
    means sending a long-lived credential on an endpoint that does not otherwise
    need one. Revoking everything and re-issuing is simpler, and it guarantees
    that a user changing their password because they fear compromise really does
    end every other session.
    """
    user = await users_service.get_active_user_for_update(db, user_id)
    if user is None:
        raise InactiveUserError
    if not await asyncio.to_thread(
        verify_password, current_password, user.password_hash
    ):
        # Distinct from the token errors: the caller is already authenticated,
        # so telling them their password was wrong reveals nothing they do not
        # know.
        raise IncorrectCurrentPasswordError
    if user.password_change_required:
        if user.temporary_password_expires_at is not None and as_utc(
            user.temporary_password_expires_at
        ) <= datetime.now(UTC):
            raise TemporaryPasswordExpiredError
        if new_password == current_password:
            raise PasswordTooWeakError(["different from the temporary password"])

    old_session = await repository.get_active_session(db, user_id, session_id)
    if old_session is None:
        raise AuthSessionExpiredError
    # A NULL address stays NULL: str(None) is "None", which is not an INET value.
    address = old_session.ip_address
    ip_address = None if address is None else str(address)
    device_info = old_session.device_info
    digest = await asyncio.to_thread(hash_password, new_password)
    await users_service.stage_password_reset(db, user_id, digest)
    await stage_revoke_all_for_user(db, user_id)
    await invalidate_outstanding(db, user_id, TokenPurpose.password_reset)
    await repository.delete_mfa_challenge(db, user_id)

    stage_password_changed_notice(db, user_id)

    claims = {
        key: value
        for key, value in carried_claims(session_claims).items()
        if key != "sid"
    }
    sid = stage_session(
        db, user_id=user_id, ip_address=ip_address, device_info=device_info
    )
    claims["sid"] = str(sid)
    if claims.get("role") == ROLE_SUPERADMIN:
        audit_client.stage(
            db,
            action="superadmin.password_changed",
            actor_id=user_id,
            target_type="user",
            target_id=user_id,
            prior_state={"sessions": "previous"},
            new_state={"sessions": "replaced"},
        )
    return await issue_pair(db, user_id, claims=claims)


async def record_request_refusal(db: AsyncSession, ip_address: str) -> None:
    """Append the outcome when an outer IP guard rejects recovery before lookup."""
    audit_service.stage_record(
        db,
        action="auth.password_reset_requested",
        actor_id=None,
        ip_address=ip_address,
        outcome="RATE_LIMIT_EXCEEDED",
    )
    await db.commit()
