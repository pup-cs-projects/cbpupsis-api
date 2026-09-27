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

**Why user rows are unannotated here.** Several functions take or return the row
``app.domains.users.service`` hands back. That type is deliberately not named:
importing ``users.models.User`` for an annotation is the boundary violation this
domain is not allowed to make, and ``UserRead`` is not a substitute because auth
needs ``password_hash``, which that DTO omits on purpose. The rows stay opaque —
read for the attributes the flows need, never queried or written directly, and
never constructed here. ``auth.dependencies`` takes the same approach.
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import emails

# Imported as a bare name, not called as ``emails.send_email``: the test suite
# captures outbound mail by patching this module's binding
# (``tests/conftest.py::sent_emails``), which only exists because of this
# from-import. Collapsing the two lines would silently un-capture every mail
# these flows send.
from app.core.emails import send_email
from app.core.events import Event
from app.core.exceptions import (
    UnauthorizedError,
)
from app.core.outbox import publish_transactional
from app.domains.auth import repository
from app.domains.auth.exceptions import (
    AccountLockedError,
    AdminProfileRequiredError,
    EmailAlreadyRegisteredError,
    InactiveUserError,
    IncorrectCurrentPasswordError,
    InvalidAuthTokenError,
    InvalidCredentialsError,
    InvalidMfaError,
    InvalidTokenError,
    MfaCodeReusedError,
    MfaEnrollmentRequiredError,
    RefreshTokenReusedError,
    UnverifiedEmailError,
)
from app.domains.auth.models import AdminPosition, AuthFailureStep, TokenPurpose
from app.domains.auth.schemas import (
    AdminLoginChallenge,
    AdminTokenPair,
    TokenPair,
    TotpEnrollmentRead,
)
from app.domains.auth.security import (
    create_access_token,
    create_mfa_challenge_token,
    create_refresh_token,
    decode_token,
    decrypt_mfa_secret,
    encrypt_mfa_secret,
    generate_one_time_token,
    generate_totp_secret,
    hash_mfa_identifier,
    hash_one_time_token,
    hash_password,
    matching_totp_counter,
    totp_provisioning_uri,
    verify_mfa_identifier,
    verify_password,
    verify_webauthn_assertion,
)
from app.domains.iam import service as iam_service
from app.domains.iam.constants import ADMIN_GROUP
from app.domains.users import service as users_service

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


async def _authenticate_login_credentials(db: AsyncSession, email: str, password: str):
    """Verify the credentials shared by ordinary and administrative login.

    The checks run in a deliberate order, and the order is the security
    property:

    1. Unknown email **or** wrong password -> 401, one identical message.
    2. Correct password, but the account is inactive or deleted -> the *same*
       401. An attacker who guesses a password must not learn from the response
       that the address exists but is disabled.
    3. Correct password, email not verified -> 403 ``email_not_verified``.
    4. Correct password, verified -> the role-specific login flow continues.

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
    if user is not None:
        await _require_not_locked(db, user.id)
    if user is None or not verify_password(password, user.password_hash):
        if user is not None:
            await _record_failure(db, user.id, AuthFailureStep.credentials)
        raise InvalidCredentialsError
    if not user.is_active or user.deleted_at is not None:
        await _record_failure(db, user.id, AuthFailureStep.credentials)
        raise InvalidCredentialsError
    _require_verified_email(user)
    return user


async def login(db: AsyncSession, email: str, password: str) -> TokenPair:
    """Issue an ordinary session from the non-admin login entry point.

    Admin credentials are refused here rather than silently issuing a session
    without MFA. Administrative callers must use ``login_admin``.
    """
    user = await _authenticate_login_credentials(db, email, password)
    if await iam_service.is_user_in_group(db, user.id, ADMIN_GROUP):
        raise InvalidCredentialsError

    await _clear_failure_state(db, user.id)
    return await _issue_pair(db, user.id)


async def login_admin(
    db: AsyncSession, email: str, password: str
) -> AdminLoginChallenge:
    """Begin an administrative login and issue only an MFA challenge.

    Non-admin credentials receive the same generic refusal as invalid
    credentials so the endpoint cannot be used to enumerate privileged users.
    """
    user = await _authenticate_login_credentials(db, email, password)

    if not await iam_service.is_user_in_group(db, user.id, ADMIN_GROUP):
        raise InvalidCredentialsError
    profile = await repository.get_admin_profile(db, user.id)
    if profile is None:
        raise AdminProfileRequiredError
    enrollment_required = not _has_enrolled_factor(profile)
    challenge_token, challenge, challenge_jti = create_mfa_challenge_token(
        user.id, enrollment_required=enrollment_required
    )
    repository.update_admin_profile(db, profile, active_mfa_challenge_jti=challenge_jti)
    await db.commit()
    methods: list[str] = []
    if profile.totp_confirmed_at is not None:
        methods.append("totp")
    if profile.webauthn_public_key_encrypted is not None:
        methods.append("webauthn")
    return AdminLoginChallenge(
        challenge_token=challenge_token,
        enrollment_required=enrollment_required,
        methods=methods,
        webauthn_challenge=challenge,
        webauthn_rp_id=settings.webauthn_rp_id,
        webauthn_credential_id=(
            decrypt_mfa_secret(profile.webauthn_credential_id_encrypted)
            if profile.webauthn_credential_id_encrypted is not None
            else None
        ),
    )


def _has_enrolled_factor(profile) -> bool:
    return bool(
        profile.totp_confirmed_at is not None
        or (
            profile.webauthn_credential_id_hash is not None
            and profile.webauthn_credential_id_encrypted is not None
            and profile.webauthn_public_key_encrypted is not None
        )
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _require_not_locked(db: AsyncSession, user_id: uuid.UUID) -> None:
    lockout = await repository.get_authentication_lockout(db, user_id)
    if lockout is None:
        return
    remaining = math.ceil(
        (_utc(lockout.locked_until) - datetime.now(UTC)).total_seconds()
    )
    if remaining > 0:
        raise AccountLockedError(remaining)
    await repository.clear_authentication_lockout(db, user_id=user_id)
    await db.commit()


async def _record_failure(
    db: AsyncSession, user_id: uuid.UUID, step: AuthFailureStep
) -> None:
    now = datetime.now(UTC)
    prior = await repository.count_authentication_failures_since(
        db,
        user_id=user_id,
        since=now - timedelta(minutes=settings.mfa_lockout_window_minutes),
    )
    repository.add_authentication_failure(
        db, user_id=user_id, step=step, occurred_at=now
    )
    if prior + 1 >= settings.mfa_lockout_threshold:
        locked_until = now + timedelta(minutes=settings.mfa_lockout_minutes)
        lockout = await repository.get_authentication_lockout(db, user_id)
        if lockout is None:
            repository.add_authentication_lockout(
                db, user_id=user_id, locked_until=locked_until
            )
            publish_transactional(
                db,
                Event(
                    name="auth.account_locked",
                    payload={
                        "user_id": str(user_id),
                        "locked_until": locked_until.isoformat(),
                    },
                ),
            )
        else:
            repository.update_authentication_lockout(
                db, lockout, locked_until=locked_until
            )
    await db.commit()


async def _clear_failure_state(db: AsyncSession, user_id: uuid.UUID) -> None:
    await repository.clear_authentication_failures(db, user_id=user_id)
    await repository.clear_authentication_lockout(db, user_id=user_id)
    await db.commit()


async def configure_admin_profile(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    position: AdminPosition,
    department_id: str | None = None,
    college_id: str | None = None,
):
    """Assign an Admin-group member's position and corresponding data scope."""
    if not await iam_service.is_user_in_group(db, user_id, ADMIN_GROUP):
        raise AdminProfileRequiredError
    if position is AdminPosition.chairperson and not department_id:
        raise ValueError("chairperson requires department_id")
    if position is AdminPosition.dean and not college_id:
        raise ValueError("dean requires college_id")
    if position is AdminPosition.dean:
        department_id = None
    if position is AdminPosition.registrar:
        department_id = None
        college_id = None

    profile = await repository.get_admin_profile(db, user_id)
    if profile is None:
        profile = repository.add_admin_profile(
            db,
            user_id=user_id,
            position=position,
            department_id=department_id,
            college_id=college_id,
        )
    else:
        repository.update_admin_profile(
            db,
            profile,
            position=position,
            department_id=department_id,
            college_id=college_id,
        )
    await db.commit()
    await db.refresh(profile)
    return profile


async def configure_webauthn_factor(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    credential_id: str,
    credential_public_key: str,
    sign_count: int = 0,
):
    """Store the verified output of a WebAuthn registration ceremony.

    Registration verification owns attestation/origin checks and passes only
    its verified credential output here. Both values are AES-256-GCM encrypted;
    a keyed digest is retained solely to enforce credential-id uniqueness.
    """
    profile = await repository.get_admin_profile(db, user_id)
    if profile is None or not await iam_service.is_user_in_group(
        db, user_id, ADMIN_GROUP
    ):
        raise AdminProfileRequiredError
    repository.update_admin_profile(
        db,
        profile,
        webauthn_credential_id_hash=hash_mfa_identifier(credential_id),
        webauthn_credential_id_encrypted=encrypt_mfa_secret(credential_id),
        webauthn_public_key_encrypted=encrypt_mfa_secret(credential_public_key),
        webauthn_sign_count=sign_count,
    )
    await db.commit()
    await db.refresh(profile)
    return profile


async def begin_totp_enrollment(
    db: AsyncSession, *, challenge_token: str
) -> TotpEnrollmentRead:
    """Create and persist an encrypted pending TOTP seed for one admin."""
    claims = decode_token(challenge_token, expected_type="mfa_challenge")
    user_id = uuid.UUID(claims["sub"])
    await _require_not_locked(db, user_id)
    if claims.get("enrollment_required") is not True:
        raise MfaEnrollmentRequiredError
    profile = await repository.get_admin_profile(db, user_id)
    if profile is None or not await iam_service.is_user_in_group(
        db, user_id, ADMIN_GROUP
    ):
        raise AdminProfileRequiredError
    _require_current_challenge(profile, claims)
    user = await users_service.get_active_user(db, user_id)
    if user is None:
        raise InactiveUserError

    secret = generate_totp_secret()
    repository.update_admin_profile(
        db,
        profile,
        totp_secret_encrypted=encrypt_mfa_secret(secret),
        totp_confirmed_at=None,
        totp_last_used_counter=None,
    )
    await db.commit()
    return TotpEnrollmentRead(
        secret=secret, provisioning_uri=totp_provisioning_uri(secret, user.email)
    )


async def confirm_totp_enrollment(
    db: AsyncSession, *, challenge_token: str, code: str
) -> AdminTokenPair:
    """Activate a pending TOTP seed only after a valid first code."""
    claims = decode_token(challenge_token, expected_type="mfa_challenge")
    user_id = uuid.UUID(claims["sub"])
    await _require_not_locked(db, user_id)
    profile = await repository.get_admin_profile(db, user_id)
    if (
        profile is None
        or profile.totp_secret_encrypted is None
        or not await iam_service.is_user_in_group(db, user_id, ADMIN_GROUP)
    ):
        raise MfaEnrollmentRequiredError
    _require_current_challenge(profile, claims)
    user = await users_service.get_active_user(db, user_id)
    if user is None:
        raise InactiveUserError
    counter = matching_totp_counter(
        decrypt_mfa_secret(profile.totp_secret_encrypted), code
    )
    if counter is None:
        await _record_failure(db, user_id, AuthFailureStep.mfa)
        raise InvalidMfaError
    repository.update_admin_profile(
        db,
        profile,
        totp_confirmed_at=datetime.now(UTC),
        totp_last_used_counter=counter,
        active_mfa_challenge_jti=None,
    )
    await db.commit()
    await _clear_failure_state(db, user_id)
    return await _issue_admin_pair(db, user_id, profile, method="totp")


async def verify_mfa(
    db: AsyncSession,
    *,
    challenge_token: str,
    code: str | None,
    assertion: dict[str, object] | None,
) -> AdminTokenPair:
    """Complete the second factor and mint the only administrative session."""
    claims = decode_token(challenge_token, expected_type="mfa_challenge")
    user_id = uuid.UUID(claims["sub"])
    await _require_not_locked(db, user_id)
    profile = await repository.get_admin_profile(db, user_id)
    if profile is None or not await iam_service.is_user_in_group(
        db, user_id, ADMIN_GROUP
    ):
        raise AdminProfileRequiredError
    _require_current_challenge(profile, claims)
    user = await users_service.get_active_user(db, user_id)
    if user is None:
        raise InactiveUserError
    if not _has_enrolled_factor(profile):
        raise MfaEnrollmentRequiredError

    method: str
    valid = False
    if code is not None and profile.totp_confirmed_at is not None:
        method = "totp"
        counter = matching_totp_counter(
            decrypt_mfa_secret(profile.totp_secret_encrypted), code
        )
        if (
            counter is not None
            and profile.totp_last_used_counter is not None
            and counter <= profile.totp_last_used_counter
        ):
            await _record_failure(db, user_id, AuthFailureStep.mfa)
            raise MfaCodeReusedError
        valid = counter is not None
        if valid:
            repository.update_admin_profile(db, profile, totp_last_used_counter=counter)
    elif (
        assertion is not None
        and profile.webauthn_credential_id_hash is not None
        and profile.webauthn_public_key_encrypted is not None
        and isinstance(assertion.get("id"), str)
        and verify_mfa_identifier(assertion["id"], profile.webauthn_credential_id_hash)
    ):
        method = "webauthn"
        new_count = verify_webauthn_assertion(
            assertion=assertion,
            challenge=claims["challenge"],
            credential_public_key=decrypt_mfa_secret(
                profile.webauthn_public_key_encrypted
            ),
            current_sign_count=profile.webauthn_sign_count,
        )
        valid = new_count is not None
        if valid:
            repository.update_admin_profile(db, profile, webauthn_sign_count=new_count)
    else:
        method = "totp" if code is not None else "webauthn"

    if not valid:
        await _record_failure(db, user_id, AuthFailureStep.mfa)
        raise InvalidMfaError

    repository.update_admin_profile(db, profile, active_mfa_challenge_jti=None)
    await db.commit()
    await _clear_failure_state(db, user_id)
    return await _issue_admin_pair(db, user_id, profile, method=method)


def _require_current_challenge(profile, claims: dict[str, object]) -> None:
    """Reject replaced or already-consumed password-step challenges."""
    if profile.active_mfa_challenge_jti != claims.get("jti"):
        raise InvalidAuthTokenError


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


async def refresh(db: AsyncSession, refresh_token: str) -> TokenPair | AdminTokenPair:
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
    claims = decode_token(refresh_token, expected_type="refresh")
    user_id = uuid.UUID(claims["sub"])

    record = await repository.get_refresh_token(db, claims["jti"])
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

    if claims.get("role") == "admin":
        if claims.get("mfa") is not True or not await iam_service.is_user_in_group(
            db, user_id, ADMIN_GROUP
        ):
            raise InvalidAuthTokenError
        profile = await repository.get_admin_profile(db, user_id)
        if profile is None or not _has_enrolled_factor(profile):
            raise MfaEnrollmentRequiredError
        amr = claims.get("amr")
        method = (
            amr[-1]
            if isinstance(amr, list) and amr and amr[-1] in {"totp", "webauthn"}
            else "totp"
        )
        return await _issue_admin_pair(db, user_id, profile, method=method)

    return await _issue_pair(db, user_id)


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


async def revoke_all_for_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Revoke every live refresh token for a user.

    Used on suspected token theft, and the right call after a password change
    or account deactivation.
    """
    await repository.revoke_all_refresh_tokens_for_user(db, user_id, datetime.now(UTC))
    await db.commit()


async def _issue_pair(db: AsyncSession, user_id: uuid.UUID) -> TokenPair:
    """Mint an access/refresh pair and record the refresh token's id."""
    access_token = create_access_token(user_id)
    refresh_token, jti, expires_at = create_refresh_token(user_id)

    repository.add_refresh_token(db, user_id, jti, expires_at)
    await db.commit()

    return TokenPair(access_token=access_token, refresh_token=refresh_token)


async def _issue_admin_pair(
    db: AsyncSession, user_id: uuid.UUID, profile, *, method: str
) -> AdminTokenPair:
    """Mint an MFA-authenticated Admin session carrying position and scope."""
    claims = {
        "role": "admin",
        "mfa": True,
        "amr": ["pwd", method],
        "position": profile.position.value,
        "department_id": profile.department_id,
        "college_id": profile.college_id,
    }
    access_token = create_access_token(user_id, claims=claims)
    refresh_token, jti, expires_at = create_refresh_token(user_id, claims=claims)
    repository.add_refresh_token(db, user_id, jti, expires_at)
    await db.commit()
    return AdminTokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        position=profile.position,
        department_id=profile.department_id,
        college_id=profile.college_id,
    )


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

    await users_service.set_password_hash(db, user_id, hash_password(new_password))
    await revoke_all_for_user(db, user_id)

    _notify_password_changed(db, user_id)

    return await _issue_pair(db, user_id)
