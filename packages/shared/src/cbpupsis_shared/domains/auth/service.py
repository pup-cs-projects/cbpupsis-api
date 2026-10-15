"""Authentication flows: register, login, refresh, logout, verification, reset.

Framework-agnostic — no Request/Response types cross into this module, so these
functions are callable from routers, scripts, and tests alike.

The refresh flow implements **rotation with reuse detection**, the standard
defence for tokens held by clients that cannot keep a secret (browsers, mobile
apps): every refresh invalidates the token presented and issues a new one, so a
stolen token has a short useful life, and a second use of an already-rotated
token is strong evidence of theft — the whole family is revoked in response.

Mailed tokens are cryptographically random, expiring, and single use.
Verification uses :func:`_consume_one_time_token`; reset consumes its token in
the same transaction as the password change. Verification shares these errors:

- **Single use.** Redemption stamps ``consumed_at`` in the same transaction that
  acts on it, so a token replayed from a browser history or a mail forward fails.
- **Expiring.** Past ``expires_at`` a token is dead regardless of use.
- **Indistinguishable failures.** Unknown, expired, already-used, and
  wrong-purpose all raise one identical error. Reporting which would tell an
  attacker holding a guessed value whether it was ever real.

No SQL lives here: the queries are in ``repository.py``. This module keeps the
transaction boundaries, because in this domain they *are* security decisions —
which is why the commits and rollbacks below sit next to the rules that motivate
them rather than inside the data-access layer.

**Why user rows are unannotated here.** Several functions take or return the
row ``cbpupsis_shared.domains.users.service`` hands back. That type is
deliberately not named:
importing ``users.models.User`` for an annotation is the boundary violation this
domain is not allowed to make, and ``UserRead`` is not a substitute because auth
needs ``password_hash``, which that DTO omits on purpose. The rows stay opaque —
read for the attributes the flows need, never queried or written directly, and
never constructed here. ``auth.dependencies`` takes the same approach.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from datetime import UTC, datetime, timedelta

from pydantic import EmailStr, TypeAdapter
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core import emails
from cbpupsis_core.config import settings

# Imported as a bare name, not called as ``emails.send_email``: the test suite
# captures outbound mail by patching this module's binding
# (``tests/conftest.py::sent_emails``), which only exists because of this
# from-import. Collapsing the two lines would silently un-capture every mail
# these flows send.
from cbpupsis_core.emails import send_email
from cbpupsis_core.events import Event
from cbpupsis_core.exceptions import (
    UnauthorizedError,
)
from cbpupsis_database.models.auth import TokenPurpose
from cbpupsis_shared.domains.audit import service as audit_service
from cbpupsis_shared.domains.auth import repository
from cbpupsis_shared.domains.auth.exceptions import (
    AuthError,
    EmailAlreadyRegisteredError,
    InactiveUserError,
    IncorrectCurrentPasswordError,
    InvalidAuthTokenError,
    InvalidCredentialsError,
    InvalidEmailAddressError,
    InvalidTokenError,
    PasswordTooWeakError,
    RefreshTokenReusedError,
    ResetRateLimitError,
    ResetTokenExpiredError,
    ResetTokenUsedError,
    UnverifiedEmailError,
)
from cbpupsis_shared.domains.auth.schemas import TokenPair
from cbpupsis_shared.domains.auth.security import (
    check_password_complexity,
    create_access_token,
    create_refresh_token,
    decode_token,
    encrypt_reset_token,
    generate_one_time_token,
    hash_one_time_token,
    hash_password,
    require_session_version,
    reset_address_hash,
    verify_password,
)
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP
from cbpupsis_shared.domains.users import service as users_service
from cbpupsis_shared.outbox import publish_transactional

logger = logging.getLogger(__name__)


async def register(
    db: AsyncSession,
    email: str,
    password: str,
    full_name: str | None = None,
):
    """Create a user account, returning the new user row.

    Relies on the unique constraint rather than a prior SELECT: checking first
    would leave a race between the check and the insert under concurrent
    signups. The integrity error is translated to a clean 409, which is why the
    insert is staged through the users domain and committed here rather than
    there.
    """
    user = users_service.stage_new_user(
        db,
        email=email.lower(),
        password_hash=hash_password(password),
        full_name=full_name,
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise EmailAlreadyRegisteredError from exc
    await db.refresh(user)

    # Best-effort: a bounced or undeliverable verification email must not undo
    # a successful signup. The user exists either way and can ask again via
    # /auth/resend-verification.
    await _send_verification_email(db, user)
    return user


async def login(db: AsyncSession, email: str, password: str) -> TokenPair:
    """Verify credentials and issue a token pair.

    The checks run in a deliberate order, and the order is the security
    property:

    1. Unknown email **or** wrong password -> 401, one identical message.
    2. Correct password, but the account is inactive or deleted -> the *same*
       401. An attacker who guesses a password must not learn from the response
       that the address exists but is disabled.
    3. Correct password, email not verified -> 403 ``email_not_verified``.
    4. Correct password, verified -> a token pair.

    **Why the 403 does not enable enumeration.** It is unreachable until the
    password has already been verified, so it only ever tells a caller who
    *already holds valid credentials* that their own address is unverified. An
    attacker probing an email list without the password sees nothing but the
    identical 401 at step 1, exactly as before. What the 403 reveals — "this
    account is registered, and you know its password" — is information the
    caller demonstrably already has. That is the standard trade every major
    product makes to gate login on verification, and it leaves the enumeration
    guarantee at steps 1 and 2 intact.

    The distinct status is what lets the frontend send the user to a "resend
    verification" screen rather than back to the login form, which is why this
    is a 403 with a stable code and not a fourth flavour of the 401.
    """
    user = await users_service.get_by_email_for_update(db, email.lower())
    if user is None or not await asyncio.to_thread(
        verify_password, password, user.password_hash
    ):
        raise InvalidCredentialsError
    if not user.is_active or user.deleted_at is not None:
        raise InvalidCredentialsError
    _require_verified_email(user)
    if await iam_service.is_user_in_group(db, user.id, ADMIN_GROUP):
        # Administrative accounts must enter through /auth/admin/login so a
        # password can never mint a privileged session without MFA.
        raise InvalidCredentialsError
    return await _issue_pair(db, user.id)


def _require_verified_email(user) -> None:
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


async def refresh(
    db: AsyncSession,
    refresh_token: str,
    *,
    session_claims: dict[str, object] | None = None,
) -> TokenPair:
    """Rotate a refresh token, returning a fresh pair.

    Raises :class:`UnauthorizedError` if the token is invalid, expired, or has
    already been used — the last case also revokes every other token for that
    user, on the assumption that a replayed token means it leaked.

    Applies the same email-verification gate as login, and for the same reason:
    refresh mints access tokens, so leaving it ungated would let a session
    started before the rule (or before the address was un-verified) keep
    renewing itself indefinitely and walk straight around the block. The account
    state is re-read from the database on every rotation rather than trusted
    from the token, which is what makes the gate take effect immediately.
    """
    token_claims = decode_token(refresh_token, expected_type="refresh")
    if token_claims.get("role") == "admin" and session_claims is None:
        raise InvalidAuthTokenError
    user_id = uuid.UUID(token_claims["sub"])

    user = await users_service.get_active_user_for_update(db, user_id)
    if user is not None:
        require_session_version(token_claims, user.session_version)
    record = await repository.get_refresh_token(db, token_claims["jti"])
    if record is None:
        raise InvalidAuthTokenError

    if record.revoked_at is not None:
        # Reuse of a rotated token: treat as compromise and cut off the family.
        await revoke_all_for_user(db, user_id)
        raise RefreshTokenReusedError

    repository.mark_refresh_token_revoked(db, record, datetime.now(UTC))
    await db.flush()

    # Confirm the account is still usable before minting new credentials.
    user = await users_service.get_active_user(db, user_id)
    if user is None:
        await db.commit()
        raise InactiveUserError

    # Committed before the gate can raise, so the token presented here stays
    # revoked either way. Letting the rollback resurrect it would hand a
    # blocked account an endlessly retryable credential.
    await db.commit()
    _require_verified_email(user)

    claims = (
        session_claims if session_claims is not None else _session_claims(token_claims)
    )
    return await _issue_pair(
        db,
        user_id,
        claims=claims,
        expected_session_version=token_claims.get("session_version", 0),
    )


async def logout(db: AsyncSession, refresh_token: str) -> None:
    """Revoke a single refresh token.

    Invalid or already-revoked tokens are accepted silently: logout should be
    idempotent, and reporting failure leaks whether a token was genuine.
    """
    try:
        claims = decode_token(refresh_token, expected_type="refresh")
    except UnauthorizedError:
        return
    await repository.revoke_refresh_token(db, claims["jti"], datetime.now(UTC))
    await db.commit()


async def revoke_all_for_user(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Revoke every live refresh token for a user.

    Used on suspected token theft, and the right call after a password change
    or account deactivation. Returns the count of revoked tokens.
    """
    await users_service.stage_invalidate_sessions(db, user_id)
    count = await repository.revoke_all_refresh_tokens_for_user(
        db, user_id, datetime.now(UTC)
    )
    await db.commit()
    return count


async def stage_revoke_all_for_user(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Stage refresh-token revocation in a caller-owned transaction."""
    await users_service.stage_invalidate_sessions(db, user_id)
    count = await repository.revoke_all_refresh_tokens_for_user(
        db, user_id, datetime.now(UTC)
    )
    return count


async def issue_session(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    claims: dict[str, object] | None = None,
) -> TokenPair:
    """Issue and persist a typed session for another authentication domain."""
    return await _issue_pair(db, user_id, claims=claims)


def _session_claims(claims: dict[str, object]) -> dict[str, object]:
    """Keep role/scope claims while dropping JWT control claims on rotation."""
    control = {"sub", "type", "iat", "exp", "jti", "session_version"}
    return {name: value for name, value in claims.items() if name not in control}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _issue_pair(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    claims: dict[str, object] | None = None,
    expected_session_version: int | None = None,
) -> TokenPair:
    """Mint a pair under the same account lock used by resets."""
    user = await users_service.get_active_user_for_update(db, user_id)
    if user is None:
        raise InactiveUserError
    if expected_session_version is not None:
        require_session_version(
            {"session_version": expected_session_version}, user.session_version
        )
    claims = {**(claims or {}), "session_version": user.session_version}
    access_token = create_access_token(user_id, claims=claims)
    refresh_token, jti, expires_at = create_refresh_token(user_id, claims=claims)

    repository.add_refresh_token(db, user_id, jti, expires_at)
    await db.commit()

    return TokenPair(access_token=access_token, refresh_token=refresh_token)


# --------------------------------------------------------------------------- #
# One-time tokens: the shared machinery behind verification and reset.
# --------------------------------------------------------------------------- #


async def _issue_one_time_token(
    db: AsyncSession, user_id: uuid.UUID, purpose: TokenPurpose, ttl: timedelta
) -> str:
    """Create a one-time token, returning the raw secret to be mailed.

    Verification resends invalidate outstanding verification tokens first.
    Password recovery uses its own queued path and keeps earlier links usable
    until a successful reset spends every outstanding reset token.

    Only the digest is stored; the returned raw value is never persisted or
    logged.
    """
    await _invalidate_outstanding(db, user_id, purpose)

    raw_token, token_hash = generate_one_time_token()
    repository.add_one_time_token(
        db,
        user_id=user_id,
        token_hash=token_hash,
        purpose=purpose,
        expires_at=datetime.now(UTC) + ttl,
    )
    await db.commit()
    return raw_token


async def _invalidate_outstanding(
    db: AsyncSession, user_id: uuid.UUID, purpose: TokenPurpose
) -> None:
    """Consume every live token a user holds for one purpose."""
    await repository.consume_outstanding_one_time_tokens(
        db, user_id, purpose, datetime.now(UTC)
    )


async def _consume_one_time_token(
    db: AsyncSession, raw_token: str, purpose: TokenPurpose
) -> uuid.UUID:
    """Redeem a mailed token, returning the user id it belongs to.

    Raises :class:`UnauthorizedError` with one identical message for every
    failure mode — unknown, wrong purpose, expired, or already consumed.

    The row is marked consumed via a conditional UPDATE rather than a
    read-then-write: ``WHERE consumed_at IS NULL`` makes the database the
    arbiter, so two requests racing with the same token produce one winner. A
    check-then-set here would let both pass the check before either wrote, and
    the token would be used twice.
    """
    token_hash = hash_one_time_token(raw_token)
    now = datetime.now(UTC)

    user_id = await repository.consume_one_time_token(db, token_hash, purpose, now)
    if user_id is None:
        await db.rollback()
        raise InvalidTokenError

    await db.commit()
    return user_id


async def _send_verification_email(db: AsyncSession, user) -> None:
    """Issue a verification token and mail it. Never raises."""
    raw_token = await _issue_one_time_token(
        db,
        user.id,
        TokenPurpose.email_verification,
        timedelta(hours=settings.email_verification_ttl_hours),
    )
    subject, body = emails.verification_email(raw_token)
    await send_email(to=user.email, subject=subject, body=body)


# --------------------------------------------------------------------------- #
# Email verification
# --------------------------------------------------------------------------- #


async def verify_email(db: AsyncSession, raw_token: str) -> None:
    """Redeem a verification token and stamp the user as verified.

    Raises :class:`UnauthorizedError` on any invalid token. A token whose user
    has since been deleted fails the same way, so a deletion cannot be detected
    by watching this endpoint's response change.
    """
    user_id = await _consume_one_time_token(
        db, raw_token, TokenPurpose.email_verification
    )
    user = await users_service.get_active_user(db, user_id)
    if user is None:
        raise InvalidTokenError
    await users_service.mark_email_verified(db, user_id)


async def resend_verification(db: AsyncSession, email: str) -> None:
    """Send a fresh verification email, if that is a meaningful thing to do.

    Returns ``None`` unconditionally. An unknown address, an inactive account,
    and an already-verified one all take the same silent path: reporting the
    difference would turn this endpoint into a registration oracle, which is the
    exact thing the identical login error exists to prevent.
    """
    user = await users_service.get_by_email(db, email.lower())
    if user is None or not user.is_active or user.email_verified_at is not None:
        logger.info("Verification resend requested for a non-eligible address")
        return
    await _send_verification_email(db, user)


# --------------------------------------------------------------------------- #
# Password reset (unauthenticated) and change (authenticated)
# --------------------------------------------------------------------------- #


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
        retry_after = max(1, math.ceil((_utc(started) + window - now).total_seconds()))
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
                    "encrypted_token": encrypt_reset_token(
                        raw_token, user_id=user.id, token_hash=token_hash
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
    if _utc(record.expires_at) <= datetime.now(UTC):
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
        await repository.clear_authentication_lockout(db, user_id)
        await _invalidate_outstanding(db, user_id, TokenPurpose.password_reset)
        _notify_password_changed(db, user_id)
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


def _notify_password_changed(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Stage the password-changed notice for durable delivery.

    Staged, not sent: the row commits with the caller's transaction, so the
    notice cannot be lost between "the password changed" and "the owner was
    told" — which is the one window where losing it matters most.
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
) -> TokenPair:
    """Change an authenticated user's password, returning a fresh token pair.

    Requires the current password: an access token alone must not be enough to
    take permanent ownership of an account.

    **All** refresh tokens are revoked, including the caller's, and a new pair
    is returned. The alternative — keeping the calling session alive — needs the
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
        # Distinct from the token errors above: the caller is already
        # authenticated, so telling them their password was wrong reveals
        # nothing they do not know.
        raise IncorrectCurrentPasswordError

    digest = await asyncio.to_thread(hash_password, new_password)
    await users_service.stage_password_reset(db, user_id, digest)
    await stage_revoke_all_for_user(db, user_id)
    await _invalidate_outstanding(db, user_id, TokenPurpose.password_reset)

    _notify_password_changed(db, user_id)

    return await _issue_pair(db, user_id)


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
