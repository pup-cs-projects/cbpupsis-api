"""Internal client for the auth domain: the extraction swap point.

Other domains depend on THIS module, never on ``service.py`` directly. Today it
forwards to the local service; if auth is ever extracted, only this file changes.

Auth's public surface to other domains is what a role-specific sign-in needs to
reuse rather than reimplement: the shared role sign-in flow (password step,
challenge, completion), the lockout, MFA factors, recovery codes, and
server-side sessions. Each role domain keeps its own credential rules (which
identifier, which profile, which factor) and nothing else.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.auth import AuthFailureStep, MfaType
from cbpupsis_shared.domains.auth import service
from cbpupsis_shared.domains.auth.exceptions import AuthError
from cbpupsis_shared.domains.auth.schemas import (
    Challenge,
    TokenPair,
    TotpEnrollment,
    WebAuthnFactor,
)
from cbpupsis_shared.domains.auth.security import (
    create_challenge_token,
    decode_challenge_token,
    decode_token,
    lockout_subject,
    require_session_version,
)
from cbpupsis_shared.domains.users.schemas import AccountCreate

__all__ = [
    "AuthFailureStep",
    "Challenge",
    "MfaType",
    "begin_totp_enrollment",
    "bind_mfa_challenge",
    "check_password",
    "check_session",
    "check_superadmin_session",
    "clear_sign_in_failures",
    "complete_sign_in",
    "create_account",
    "create_accounts",
    "create_challenge_token",
    "decode_access_session",
    "decode_challenge_token",
    "decode_refresh_session",
    "enrolled_factors",
    "expiring_pair",
    "get_webauthn_factor",
    "issue_recovery_codes",
    "issue_role_session",
    "lockout_subject",
    "open_challenge",
    "record_attempt",
    "record_sign_in_failure",
    "record_webauthn_use",
    "redeem_recovery_code",
    "refresh_role_session",
    "refuse_expired_temporary_password",
    "reissue_temporary_password",
    "require_bound_challenge",
    "require_not_locked",
    "require_superadmin_actor",
    "retire_mfa_challenge",
    "revoke_all_sessions",
    "revoke_refresh_token",
    "stage_revoke_all_sessions",
    "stage_webauthn_factor",
    "validate_session_version",
    "verify_identifier_password",
    "verify_totp",
    "webauthn_credential_owner",
]


async def revoke_all_sessions(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Revoke every live refresh token and session for a user."""
    await service.revoke_all_for_user(db, user_id)


async def stage_revoke_all_sessions(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Stage session revocation for a transaction owned by another domain."""
    await service.stage_revoke_all_for_user(db, user_id)


async def check_password(raw_password: str, password_hash: str | None) -> bool:
    """Return whether a raw password matches a stored hash, off the event loop.

    Pass ``None`` when no account matched: the check still costs a full hash,
    so timing does not reveal which identifiers exist.
    """
    return await service.check_password(raw_password, password_hash)


def decode_access_session(token: str) -> dict[str, object]:
    """Validate an access token and expose its typed session claims."""
    return decode_token(token, expected_type="access")


def decode_refresh_session(token: str) -> dict[str, object]:
    """Validate a refresh token before a role-specific rotation begins."""
    return decode_token(token, expected_type="refresh")


def validate_session_version(claims: dict[str, object], current_version: int) -> None:
    """Apply account-wide access revocation to every role's session guard."""
    require_session_version(claims, current_version)


async def issue_role_session(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    role: str,
    ip_address: str | None,
    device_info: str = "",
    mfa: bool = False,
    claims: dict[str, object] | None = None,
    commit: bool = True,
) -> TokenPair:
    """Open a server-side session for an authenticated user and issue tokens."""
    return await service.issue_role_session(
        db,
        user_id,
        role=role,
        ip_address=ip_address,
        device_info=device_info,
        mfa=mfa,
        claims=claims,
        commit=commit,
    )


async def refresh_role_session(
    db: AsyncSession,
    refresh_token: str,
    *,
    session_claims: dict[str, object],
    commit: bool = True,
) -> TokenPair:
    """Rotate a role session's refresh token with freshly checked claims."""
    return await service.refresh(
        db, refresh_token, session_claims=session_claims, commit=commit
    )


async def check_session(
    db: AsyncSession,
    claims: dict,
    *,
    touch: bool = True,
    allow_password_change: bool = False,
) -> uuid.UUID:
    return await service.check_session(
        db, claims, touch=touch, allow_password_change=allow_password_change
    )


async def check_superadmin_session(
    db: AsyncSession,
    claims: dict,
    *,
    touch: bool = True,
    allow_password_change: bool = False,
) -> uuid.UUID:
    return await service.check_superadmin_session(
        db, claims, touch=touch, allow_password_change=allow_password_change
    )


async def revoke_refresh_token(db: AsyncSession, refresh_token: str) -> None:
    """End one session and revoke its refresh token. Idempotent."""
    await service.logout(db, refresh_token)


async def require_superadmin_actor(db: AsyncSession, user_id: uuid.UUID) -> None:
    await service.require_superadmin_actor(db, user_id)


async def require_not_locked(db: AsyncSession, subject_id: uuid.UUID) -> None:
    await service.require_not_locked(db, subject_id)


async def record_sign_in_failure(
    db: AsyncSession,
    subject_id: uuid.UUID,
    step: AuthFailureStep,
    *,
    notify: bool = True,
) -> None:
    await service.record_sign_in_failure(db, subject_id, step, notify=notify)


async def clear_sign_in_failures(db: AsyncSession, subject_id: uuid.UUID) -> None:
    await service.clear_sign_in_failures(db, subject_id)


async def bind_mfa_challenge(db: AsyncSession, user_id: uuid.UUID, jti: str) -> None:
    await service.bind_mfa_challenge(db, user_id, jti)


async def require_bound_challenge(
    db: AsyncSession, user_id: uuid.UUID, jti: object
) -> None:
    await service.require_bound_challenge(db, user_id, jti)


async def retire_mfa_challenge(db: AsyncSession, user_id: uuid.UUID) -> None:
    await service.retire_mfa_challenge(db, user_id)


async def enrolled_factors(db: AsyncSession, user_id: uuid.UUID) -> list[MfaType]:
    return await service.enrolled_factors(db, user_id)


async def begin_totp_enrollment(
    db: AsyncSession, user_id: uuid.UUID, account_name: str
) -> TotpEnrollment:
    return await service.begin_totp_enrollment(db, user_id, account_name)


async def verify_totp(
    db: AsyncSession, user_id: uuid.UUID, code: str, *, enrolling: bool
) -> None:
    await service.verify_totp(db, user_id, code, enrolling=enrolling)


async def get_webauthn_factor(
    db: AsyncSession, user_id: uuid.UUID
) -> WebAuthnFactor | None:
    return await service.get_webauthn_factor(db, user_id)


async def stage_webauthn_factor(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    credential_id: str,
    public_key: str,
    sign_count: int,
) -> None:
    await service.stage_webauthn_factor(
        db,
        user_id,
        credential_id=credential_id,
        public_key=public_key,
        sign_count=sign_count,
    )


async def record_webauthn_use(
    db: AsyncSession, user_id: uuid.UUID, sign_count: int
) -> None:
    await service.record_webauthn_use(db, user_id, sign_count)


async def webauthn_credential_owner(
    db: AsyncSession, credential_id: str
) -> uuid.UUID | None:
    return await service.webauthn_credential_owner(db, credential_id)


async def issue_recovery_codes(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    return await service.issue_recovery_codes(db, user_id)


async def redeem_recovery_code(db: AsyncSession, user_id: uuid.UUID, code: str) -> None:
    await service.redeem_recovery_code(db, user_id, code)


async def create_account(
    db: AsyncSession, *, actor_id: uuid.UUID, account: AccountCreate
):
    """Create one Superadmin-issued account; returns the user row."""
    return await service.create_account(db, actor_id=actor_id, account=account)


async def create_accounts(
    db: AsyncSession, *, actor_id: uuid.UUID, accounts: Sequence[AccountCreate]
) -> list[uuid.UUID]:
    return await service.create_accounts(db, actor_id=actor_id, accounts=accounts)


async def reissue_temporary_password(
    db: AsyncSession, *, actor_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    await service.reissue_temporary_password(db, actor_id=actor_id, user_id=user_id)


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
    await service.refuse_expired_temporary_password(
        db,
        user_id=user_id,
        password_change_required=password_change_required,
        expires_at=expires_at,
        attempted_id=attempted_id,
        ip_address=ip_address,
        device_info=device_info,
    )


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
    service.record_attempt(
        db,
        attempted_id=attempted_id,
        user_id=user_id,
        ip_address=ip_address,
        device_info=device_info,
        failure_reason=failure_reason,
        mfa_used=mfa_used,
    )


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
    """Run a role's identifier-and-password step under the shared lockout."""
    return await service.verify_identifier_password(
        db,
        identifier=identifier,
        subject=subject,
        password=password,
        ip_address=ip_address,
        device_info=device_info,
        reject=reject,
        failure=failure,
    )


async def open_challenge[P](
    db: AsyncSession,
    token: str,
    *,
    role: str,
    load_profile: Callable[[AsyncSession, uuid.UUID], Awaitable[P]],
) -> Challenge[P]:
    """Accept a role's MFA challenge token, checking the role's live profile."""
    return await service.open_challenge(db, token, role=role, load_profile=load_profile)


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
    """Record the completed sign-in and open the role's session."""
    return await service.complete_sign_in(
        db,
        user_id,
        role=role,
        attempted_id=attempted_id,
        ip_address=ip_address,
        device_info=device_info,
        mfa=mfa,
        claims=claims,
        commit=commit,
    )


def expiring_pair[T: TokenPair](schema: type[T], pair: TokenPair) -> T:
    return service.expiring_pair(schema, pair)
