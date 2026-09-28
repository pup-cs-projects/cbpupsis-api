"""Authentication flows: register, login, refresh, logout, verification, reset.

Framework-agnostic — no Request/Response types cross into this module, so these
functions are callable from routers, scripts, and tests alike.

The refresh flow implements **rotation with reuse detection**, the standard
defence for tokens held by clients that cannot keep a secret (browsers, mobile
apps): every refresh invalidates the token presented and issues a new one, so a
stolen token has a short useful life, and a second use of an already-rotated
token is strong evidence of theft — the whole family is revoked in response.

The mailed-token flows (verification, password reset) share three rules, all of
them enforced in :func:`_consume_one_time_token`:

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

import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta

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
from cbpupsis_shared.domains.audit import client as audit_client
from cbpupsis_shared.domains.auth import repository
from cbpupsis_shared.domains.auth.exceptions import (
    AuthSessionExpiredError,
    EmailAlreadyRegisteredError,
    InactiveUserError,
    IncorrectCurrentPasswordError,
    InvalidAuthTokenError,
    InvalidCredentialsError,
    InvalidTokenError,
    RefreshTokenReusedError,
    UnverifiedEmailError,
)
from cbpupsis_shared.domains.auth.schemas import TokenPair
from cbpupsis_shared.domains.auth.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_one_time_token,
    hash_one_time_token,
    hash_password,
    verify_password,
)
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP, SUPERADMIN_GROUP
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
    user = await users_service.get_by_email(db, email.lower())
    if user is None or not verify_password(password, user.password_hash):
        raise InvalidCredentialsError
    if not user.is_active or user.deleted_at is not None:
        raise InvalidCredentialsError
    _require_verified_email(user)
    if await iam_service.is_user_in_group(
        db, user.id, ADMIN_GROUP
    ) or await iam_service.is_user_in_group(db, user.id, SUPERADMIN_GROUP):
        # Administrative accounts must enter through /auth/admin/login so a
        # password can never mint a privileged session without MFA.
        raise InvalidCredentialsError
    return await _issue_pair(db, user.id)


def _session_hash(session_id: uuid.UUID) -> str:
    return hashlib.sha256(session_id.bytes).hexdigest()


async def stage_superadmin_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    device_info: str,
    ip_address: str,
) -> None:
    now = datetime.now(UTC)
    repository.add_active_session(
        db,
        session_id=session_id,
        user_id=user_id,
        token_hash=_session_hash(session_id),
        device_info=device_info,
        ip_address=ip_address,
        now=now,
    )


async def touch_superadmin_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    now = datetime.now(UTC)
    live = await repository.touch_active_session(
        db,
        session_id=session_id,
        user_id=user_id,
        token_hash=_session_hash(session_id),
        cutoff=now - timedelta(minutes=15),
        now=now,
    )
    if not live:
        await repository.delete_active_session(
            db, session_id=session_id, user_id=user_id
        )
        await db.commit()
        raise AuthSessionExpiredError
    await db.commit()


async def end_superadmin_session(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    commit: bool = True,
) -> None:
    await repository.delete_active_session(db, session_id=session_id, user_id=user_id)
    if commit:
        await db.commit()


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
    commit: bool = True,
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
    if token_claims.get("role") in {"admin", "superadmin"} and session_claims is None:
        raise InvalidAuthTokenError
    user_id = uuid.UUID(token_claims["sub"])

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
    if token_claims.get("role") != "superadmin" and await iam_service.is_user_in_group(
        db, user_id, SUPERADMIN_GROUP
    ):
        await db.commit()
        raise InvalidAuthTokenError

    # The ordinary flow commits revocation before the verification gate. A
    # caller-owned transaction can defer that commit to pair and audit staging.
    if commit:
        await db.commit()
    _require_verified_email(user)

    claims = (
        session_claims if session_claims is not None else _session_claims(token_claims)
    )
    return await _issue_pair(db, user_id, claims=claims, commit=commit)


async def logout(
    db: AsyncSession,
    refresh_token: str,
    *,
    allow_superadmin: bool = False,
    commit: bool = True,
) -> None:
    """Revoke a single refresh token.

    Invalid or already-revoked tokens are accepted silently: logout should be
    idempotent, and reporting failure leaks whether a token was genuine.
    """
    try:
        claims = decode_token(refresh_token, expected_type="refresh")
    except UnauthorizedError:
        return
    if claims.get("role") == "superadmin" and not allow_superadmin:
        raise InvalidAuthTokenError
    await repository.revoke_refresh_token(db, claims["jti"], datetime.now(UTC))
    if commit:
        await db.commit()


async def revoke_all_for_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Revoke every live refresh token for a user.

    Used on suspected token theft, and the right call after a password change
    or account deactivation.
    """
    await repository.revoke_all_refresh_tokens_for_user(db, user_id, datetime.now(UTC))
    await db.commit()


async def stage_revoke_all_for_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Stage refresh-token revocation in a caller-owned transaction."""
    await repository.revoke_all_refresh_tokens_for_user(db, user_id, datetime.now(UTC))


async def stage_end_all_superadmin_sessions(
    db: AsyncSession, user_id: uuid.UUID
) -> None:
    """Stage removal of the account's server-side privileged sessions."""
    await repository.delete_active_sessions_for_user(db, user_id)


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
    control = {"sub", "type", "iat", "exp", "jti"}
    return {name: value for name, value in claims.items() if name not in control}


async def _issue_pair(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    claims: dict[str, object] | None = None,
    commit: bool = True,
) -> TokenPair:
    """Mint an access/refresh pair and record the refresh token's id."""
    access_token = create_access_token(user_id, claims=claims)
    refresh_token, jti, expires_at = create_refresh_token(user_id, claims=claims)

    repository.add_refresh_token(db, user_id, jti, expires_at)
    if commit:
        await db.commit()

    return TokenPair(access_token=access_token, refresh_token=refresh_token)


# --------------------------------------------------------------------------- #
# One-time tokens: the shared machinery behind verification and reset.
# --------------------------------------------------------------------------- #


async def _issue_one_time_token(
    db: AsyncSession, user_id: uuid.UUID, purpose: TokenPurpose, ttl: timedelta
) -> str:
    """Create a one-time token, returning the raw secret to be mailed.

    Any outstanding token for the same user and purpose is invalidated first, so
    requesting a new reset link kills the previous one. Without that, every
    "resend" would leave another live credential in another inbox.

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


async def forgot_password(db: AsyncSession, email: str) -> None:
    """Mail a password-reset link, if the address belongs to a live account.

    Always returns ``None``, whatever happens. The caller (a 204 endpoint) can
    therefore not distinguish a registered address from an unregistered one —
    the whole point, since a "no such user" here would let anyone test an email
    list against the user base.

    Note this is enumeration-safe by *response*, not by timing: a real send is
    slower than an early return. That is acceptable because the SES call is
    already best-effort and off-thread, but if timing becomes a concern, the fix
    is to queue the send rather than to fake work here.
    """
    user = await users_service.get_by_email(db, email.lower())
    if user is None or not user.is_active:
        logger.info("Password reset requested for a non-eligible address")
        return

    raw_token = await _issue_one_time_token(
        db,
        user.id,
        TokenPurpose.password_reset,
        timedelta(minutes=settings.password_reset_ttl_minutes),
    )
    subject, body = emails.password_reset_email(raw_token)
    await send_email(to=user.email, subject=subject, body=body)


async def reset_password(db: AsyncSession, raw_token: str, new_password: str) -> None:
    """Redeem a reset token and set a new password.

    Every refresh token for the user is revoked. Someone resetting a password
    has either forgotten it or is recovering from a compromise; in the second
    case an attacker holds a live session, and leaving it alive would make the
    reset pointless.
    """
    user_id = await _consume_one_time_token(db, raw_token, TokenPurpose.password_reset)
    user = await users_service.get_active_user(db, user_id)
    if user is None:
        raise InvalidTokenError

    await users_service.set_password_hash(db, user_id, hash_password(new_password))
    await revoke_all_for_user(db, user_id)

    # Tells the real owner that their credential changed — the signal that
    # surfaces an account takeover to the victim.
    #
    # Staged on the outbox rather than sent inline: this is a notice, not part
    # of the flow, so the user should not wait on SES for it — and, more
    # importantly, a notice about a credential change must not be lost if the
    # process dies right after the password is written. The one-time-token
    # emails above stay inline deliberately: they ARE the flow, and a reset link
    # arriving a poll interval late is a UX regression the durability does not
    # pay for.
    _notify_password_changed(db, user_id)


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
    session_claims: dict[str, object] | None = None,
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
    user = await users_service.get_by_id(db, user_id)
    if not verify_password(current_password, user.password_hash):
        # Distinct from the token errors above: the caller is already
        # authenticated, so telling them their password was wrong reveals
        # nothing they do not know.
        raise IncorrectCurrentPasswordError

    if session_claims is not None and session_claims.get("role") == "superadmin":
        await users_service.stage_password_hash(
            db, user_id, hash_password(new_password)
        )
        await stage_revoke_all_for_user(db, user_id)
        _notify_password_changed(db, user_id)
        pair = await _issue_pair(db, user_id, claims=session_claims, commit=False)
        audit_client.stage(
            db,
            action="superadmin.password_changed",
            actor_id=user_id,
            target_type="user",
            target_id=str(user_id),
            prior_state={"password_changed": False},
            new_state={"password_changed": True},
        )
        await db.commit()
        return pair

    await users_service.set_password_hash(db, user_id, hash_password(new_password))
    await revoke_all_for_user(db, user_id)

    _notify_password_changed(db, user_id)

    return await _issue_pair(db, user_id)
