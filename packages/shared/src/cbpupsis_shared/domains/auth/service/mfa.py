"""MFA challenges and factors, shared by every role that uses a second factor.

The WebAuthn ceremony itself (options, signature checks) lives in the Admin
app; this module only stores and reads the enrolled key.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.auth import AuthFailureStep, MfaType
from cbpupsis_shared.domains.auth import repository
from cbpupsis_shared.domains.auth.exceptions import (
    InvalidAuthTokenError,
    InvalidMfaError,
    MfaCodeReusedError,
    MfaEnrollmentRequiredError,
)
from cbpupsis_shared.domains.auth.schemas import TotpEnrollment, WebAuthnFactor
from cbpupsis_shared.domains.auth.security import (
    credential_id_digest,
    decrypt_secret,
    encrypt_secret,
    generate_recovery_codes,
    generate_totp_secret,
    hash_recovery_code,
    matching_totp_counter,
    totp_provisioning_uri,
)
from cbpupsis_shared.domains.auth.service.lockout import record_sign_in_failure


async def bind_mfa_challenge(db: AsyncSession, user_id: uuid.UUID, jti: str) -> None:
    """Stage ``jti`` as the account's only acceptable challenge."""
    challenge = await repository.get_mfa_challenge(db, user_id)
    if challenge is None:
        repository.add_mfa_challenge(db, user_id=user_id, jti=jti)
    else:
        repository.update_mfa_challenge(challenge, jti=jti)


async def require_bound_challenge(
    db: AsyncSession, user_id: uuid.UUID, jti: object
) -> None:
    """Refuse a challenge token that is not the account's latest one."""
    challenge = await repository.get_mfa_challenge(db, user_id)
    if challenge is None or challenge.active_jti != jti:
        raise InvalidAuthTokenError


async def retire_mfa_challenge(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Stage removal of the account's live challenge."""
    await repository.delete_mfa_challenge(db, user_id)


async def enrolled_factors(db: AsyncSession, user_id: uuid.UUID) -> list[MfaType]:
    """Return the factor types the user has finished enrolling."""
    factors = []
    for mfa_type in MfaType:
        credential = await repository.get_mfa_credential(
            db, user_id=user_id, mfa_type=mfa_type
        )
        if credential is not None and credential.is_enrolled:
            factors.append(mfa_type)
    return factors


async def begin_totp_enrollment(
    db: AsyncSession, user_id: uuid.UUID, account_name: str
) -> TotpEnrollment:
    """Stage a fresh pending TOTP seed; refuse if TOTP is already enrolled.

    Refusing is what stops a password-only holder from replacing an enrolled
    authenticator with their own.
    """
    credential = await repository.get_mfa_credential(
        db, user_id=user_id, mfa_type=MfaType.totp
    )
    if credential is not None and credential.is_enrolled:
        raise MfaEnrollmentRequiredError
    secret = generate_totp_secret()
    if credential is None:
        repository.add_mfa_credential(
            db,
            user_id=user_id,
            mfa_type=MfaType.totp,
            totp_secret=encrypt_secret(secret),
            is_enrolled=False,
        )
    else:
        repository.update_mfa_credential(
            credential,
            totp_secret=encrypt_secret(secret),
            totp_last_counter=None,
            last_used_at=None,
        )
    return TotpEnrollment(
        secret=secret, provisioning_uri=totp_provisioning_uri(secret, account_name)
    )


async def verify_totp(
    db: AsyncSession,
    user_id: uuid.UUID,
    code: str,
    *,
    enrolling: bool,
    notify_lock: bool = True,
) -> None:
    """Accept one TOTP code, or record the failure and raise.

    ``enrolling`` confirms a pending seed; otherwise the factor must already be
    enrolled. A code at or below the last accepted step is a replay and counts
    as a failure like a wrong code. Success is staged, not committed.
    """
    credential = await repository.get_mfa_credential(
        db, user_id=user_id, mfa_type=MfaType.totp
    )
    if (
        credential is None
        or credential.totp_secret is None
        or credential.is_enrolled is enrolling
    ):
        raise MfaEnrollmentRequiredError
    counter = matching_totp_counter(decrypt_secret(credential.totp_secret), code)
    if counter is None:
        await record_sign_in_failure(
            db, user_id, AuthFailureStep.mfa, notify=notify_lock
        )
        raise InvalidMfaError
    last = credential.totp_last_counter
    if last is not None and counter <= last:
        await record_sign_in_failure(
            db, user_id, AuthFailureStep.mfa, notify=notify_lock
        )
        raise MfaCodeReusedError
    repository.update_mfa_credential(
        credential,
        is_enrolled=True,
        totp_last_counter=counter,
        last_used_at=datetime.now(UTC),
    )


async def get_webauthn_factor(
    db: AsyncSession, user_id: uuid.UUID
) -> WebAuthnFactor | None:
    """Return the user's enrolled hardware key, decrypted, or ``None``."""
    credential = await repository.get_mfa_credential(
        db, user_id=user_id, mfa_type=MfaType.webauthn
    )
    if (
        credential is None
        or not credential.is_enrolled
        or credential.credential_id is None
        or credential.public_key is None
    ):
        return None
    return WebAuthnFactor(
        credential_id=decrypt_secret(credential.credential_id),
        public_key=decrypt_secret(credential.public_key),
        sign_count=credential.sign_count,
    )


async def stage_webauthn_factor(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    credential_id: str,
    public_key: str,
    sign_count: int,
) -> None:
    """Stage a verified hardware key as the user's enrolled WebAuthn factor."""
    fields = {
        "credential_id": encrypt_secret(credential_id),
        "credential_id_digest": credential_id_digest(credential_id),
        "public_key": encrypt_secret(public_key),
        "sign_count": sign_count,
        "is_enrolled": True,
    }
    existing = await repository.get_mfa_credential(
        db, user_id=user_id, mfa_type=MfaType.webauthn
    )
    if existing is None:
        repository.add_mfa_credential(
            db, user_id=user_id, mfa_type=MfaType.webauthn, **fields
        )
    else:
        repository.update_mfa_credential(existing, **fields)


async def record_webauthn_use(
    db: AsyncSession, user_id: uuid.UUID, sign_count: int
) -> None:
    """Stage the authenticator's new signature counter after an assertion."""
    credential = await repository.get_mfa_credential(
        db, user_id=user_id, mfa_type=MfaType.webauthn
    )
    if credential is not None:
        repository.update_mfa_credential(
            credential, sign_count=sign_count, last_used_at=datetime.now(UTC)
        )


async def webauthn_credential_owner(
    db: AsyncSession, credential_id: str
) -> uuid.UUID | None:
    """Return who already registered this hardware key, if anyone."""
    credential = await repository.get_mfa_credential_by_digest(
        db, credential_id_digest(credential_id)
    )
    return None if credential is None else credential.user_id


async def issue_recovery_codes(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    """Replace the user's recovery codes; return the raw codes, shown once."""
    await repository.delete_recovery_codes(db, user_id)
    codes = generate_recovery_codes()
    for code in codes:
        repository.add_recovery_code(
            db, user_id=user_id, code_hash=hash_recovery_code(code)
        )
    return codes


async def redeem_recovery_code(db: AsyncSession, user_id: uuid.UUID, code: str) -> None:
    """Spend one recovery code, or record the failure and raise."""
    spent = await repository.consume_recovery_code(
        db,
        user_id=user_id,
        code_hash=hash_recovery_code(code),
        used_at=datetime.now(UTC),
    )
    if not spent:
        await record_sign_in_failure(db, user_id, AuthFailureStep.mfa)
        raise InvalidMfaError
