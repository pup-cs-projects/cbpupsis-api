"""Admin credential, factor, lockout, and scoped-session rules."""

from __future__ import annotations

import hmac
import math
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.admin_auth import repository
from cbpupsis_api_admin.domains.admin_auth.exceptions import (
    AccountLockedError,
    AdminProfileRequiredError,
    InvalidMfaError,
    MfaCodeReusedError,
    MfaEnrollmentRequiredError,
)
from cbpupsis_api_admin.domains.admin_auth.schemas import (
    AdminLoginChallenge,
    AdminTokenPair,
    SuperadminTokenPair,
    TotpEnrollmentRead,
)
from cbpupsis_api_admin.domains.admin_auth.security import (
    create_challenge_token,
    credential_id_digest,
    decode_challenge_token,
    decrypt_secret,
    encrypt_secret,
    generate_totp_secret,
    matching_totp_counter,
    totp_provisioning_uri,
    verify_webauthn_assertion,
)
from cbpupsis_core.config import settings
from cbpupsis_core.events import Event
from cbpupsis_database.models.admin_auth import (
    AdminPosition,
    AdminProfile,
    AuthFailureStep,
    MfaCredential,
    MfaType,
)
from cbpupsis_shared.domains.audit import client as audit_client
from cbpupsis_shared.domains.auth import client as auth_client
from cbpupsis_shared.domains.auth import service as auth_service
from cbpupsis_shared.domains.auth.exceptions import (
    InactiveUserError,
    InvalidAuthTokenError,
    InvalidCredentialsError,
    UnverifiedEmailError,
)
from cbpupsis_shared.domains.auth.schemas import RefreshRequest
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP, SUPERADMIN_GROUP
from cbpupsis_shared.domains.users import service as users_service
from cbpupsis_shared.outbox import publish_transactional


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def login_admin(
    db: AsyncSession, *, email: str, password: str
) -> AdminLoginChallenge:
    """Verify an Admin password and return only a non-privileged challenge."""
    user = await users_service.get_by_email(db, email.lower())
    if user is None:
        raise InvalidCredentialsError

    is_admin = await iam_service.is_user_in_group(db, user.id, ADMIN_GROUP)
    if await iam_service.is_user_in_group(db, user.id, SUPERADMIN_GROUP):
        raise InvalidCredentialsError
    if is_admin:
        await _require_not_locked(db, user.id)

    password_valid = auth_client.check_password(password, user.password_hash)
    if not password_valid or not user.is_active or user.deleted_at is not None:
        if is_admin:
            await _record_failure(db, user.id, AuthFailureStep.credentials)
        raise InvalidCredentialsError
    if not is_admin:
        raise InvalidCredentialsError
    if user.email_verified_at is None:
        raise UnverifiedEmailError("Verify your email address to continue")

    profile = await repository.get_admin_profile(db, user.id)
    if profile is None or not profile.is_active:
        raise AdminProfileRequiredError

    credentials = await _factor_credentials(db, user.id)
    enrollment_required = not any(c.is_enrolled for c in credentials.values())
    token, webauthn_challenge, jti = create_challenge_token(
        user.id, enrollment_required=enrollment_required
    )
    repository.update_admin_profile(db, profile, active_mfa_challenge_jti=jti)
    await db.commit()

    methods = [
        name.value for name, credential in credentials.items() if credential.is_enrolled
    ]
    webauthn = credentials.get(MfaType.webauthn)
    return AdminLoginChallenge(
        challenge_token=token,
        enrollment_required=enrollment_required,
        methods=methods,
        webauthn_challenge=webauthn_challenge,
        webauthn_rp_id=settings.webauthn_rp_id,
        webauthn_credential_id=(
            decrypt_secret(webauthn.credential_id)
            if webauthn is not None
            and webauthn.is_enrolled
            and webauthn.credential_id is not None
            else None
        ),
    )


async def begin_totp_enrollment(
    db: AsyncSession, *, challenge_token: str
) -> TotpEnrollmentRead:
    """Create an encrypted pending seed for Google Authenticator enrollment."""
    claims, user_id, _profile = await _challenge_context(db, challenge_token)
    if claims.get("enrollment_required") is not True:
        raise MfaEnrollmentRequiredError

    credential = await repository.get_mfa_credential(
        db, user_id=user_id, mfa_type=MfaType.totp
    )
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
            db,
            credential,
            totp_secret=encrypt_secret(secret),
            is_enrolled=False,
            last_used_at=None,
        )
    user = await users_service.get_active_user(db, user_id)
    if user is None:
        raise InactiveUserError
    await db.commit()
    return TotpEnrollmentRead(
        secret=secret,
        provisioning_uri=totp_provisioning_uri(secret, user.email),
    )


async def confirm_totp_enrollment(
    db: AsyncSession, *, challenge_token: str, code: str
) -> AdminTokenPair:
    """Prove the pending seed and issue the first Admin session."""
    claims, user_id, profile = await _challenge_context(db, challenge_token)
    if claims.get("enrollment_required") is not True:
        raise MfaEnrollmentRequiredError
    credential = await repository.get_mfa_credential(
        db, user_id=user_id, mfa_type=MfaType.totp
    )
    if credential is None or credential.is_enrolled or credential.totp_secret is None:
        raise MfaEnrollmentRequiredError

    counter = matching_totp_counter(decrypt_secret(credential.totp_secret), code)
    if counter is None:
        await _record_failure(db, user_id, AuthFailureStep.mfa)
        raise InvalidMfaError

    repository.update_mfa_credential(
        db,
        credential,
        is_enrolled=True,
        last_used_at=datetime.fromtimestamp(counter * 30, UTC),
    )
    repository.update_admin_profile(db, profile, active_mfa_challenge_jti=None)
    await _clear_failure_state(db, user_id)
    return await _issue_admin_pair(db, user_id, profile)


async def verify_mfa(
    db: AsyncSession,
    *,
    challenge_token: str,
    code: str | None,
    assertion: dict[str, object] | None,
) -> AdminTokenPair:
    """Complete a challenge using TOTP or a verified hardware-key assertion."""
    claims, user_id, profile = await _challenge_context(db, challenge_token)
    if claims.get("enrollment_required") is True:
        raise MfaEnrollmentRequiredError

    valid = await _verify_factor(db, user_id, claims, code, assertion)
    if not valid:
        await _record_failure(db, user_id, AuthFailureStep.mfa)
        raise InvalidMfaError

    repository.update_admin_profile(db, profile, active_mfa_challenge_jti=None)
    await _clear_failure_state(db, user_id)
    return await _issue_admin_pair(db, user_id, profile)


async def _verify_factor(
    db: AsyncSession,
    user_id: uuid.UUID,
    claims: dict[str, object],
    code: str | None,
    assertion: dict[str, object] | None,
) -> bool:
    """Verify and consume one enrolled factor for either privileged role."""
    valid = False
    if code is not None:
        credential = await repository.get_mfa_credential(
            db, user_id=user_id, mfa_type=MfaType.totp
        )
        if credential is not None and credential.is_enrolled and credential.totp_secret:
            counter = matching_totp_counter(
                decrypt_secret(credential.totp_secret), code
            )
            last_counter = (
                int(_utc(credential.last_used_at).timestamp()) // 30
                if credential.last_used_at is not None
                else None
            )
            if (
                counter is not None
                and last_counter is not None
                and counter <= last_counter
            ):
                await _record_failure(db, user_id, AuthFailureStep.mfa)
                raise MfaCodeReusedError
            valid = counter is not None
            if valid:
                repository.update_mfa_credential(
                    db,
                    credential,
                    last_used_at=datetime.fromtimestamp(counter * 30, UTC),
                )
    elif assertion is not None:
        credential = await repository.get_mfa_credential(
            db, user_id=user_id, mfa_type=MfaType.webauthn
        )
        assertion_id = assertion.get("id")
        if (
            credential is not None
            and credential.is_enrolled
            and credential.credential_id is not None
            and credential.public_key is not None
            and isinstance(assertion_id, str)
            and hmac.compare_digest(
                assertion_id, decrypt_secret(credential.credential_id)
            )
        ):
            new_count = verify_webauthn_assertion(
                assertion=assertion,
                challenge=str(claims["challenge"]),
                credential_public_key=decrypt_secret(credential.public_key),
                current_sign_count=credential.sign_count,
            )
            valid = new_count is not None
            if valid:
                repository.update_mfa_credential(
                    db, credential, sign_count=new_count, last_used_at=datetime.now(UTC)
                )

    return valid


async def login_superadmin(
    db: AsyncSession, *, email: str, password: str
) -> AdminLoginChallenge:
    user = await users_service.get_by_email(db, email.lower())
    if user is None:
        raise InvalidCredentialsError
    is_superadmin = await iam_service.is_user_in_group(db, user.id, SUPERADMIN_GROUP)
    if is_superadmin:
        await _require_not_locked(db, user.id)
    password_valid = auth_client.check_password(password, user.password_hash)
    if not password_valid or not user.is_active or user.deleted_at is not None:
        if is_superadmin:
            await _record_failure(db, user.id, AuthFailureStep.credentials)
        raise InvalidCredentialsError
    if not is_superadmin or await iam_service.is_user_in_group(
        db, user.id, ADMIN_GROUP
    ):
        raise InvalidCredentialsError
    if user.email_verified_at is None:
        raise UnverifiedEmailError("Verify your email address to continue")

    credentials = await _factor_credentials(db, user.id)
    enrollment_required = not any(c.is_enrolled for c in credentials.values())
    token, webauthn_challenge, jti = create_challenge_token(
        user.id, enrollment_required=enrollment_required, role="superadmin"
    )
    existing = await repository.get_superadmin_challenge(db, user.id)
    repository.stage_superadmin_challenge(db, user.id, jti, existing)
    audit_client.stage(
        db,
        action="superadmin.login_challenge",
        actor_id=user.id,
        target_type="session",
        target_id=str(user.id),
        prior_state={"mfa_pending": False},
        new_state={"mfa_pending": True},
    )
    await db.commit()
    methods = [
        name.value for name, credential in credentials.items() if credential.is_enrolled
    ]
    webauthn = credentials.get(MfaType.webauthn)
    return AdminLoginChallenge(
        challenge_token=token,
        enrollment_required=enrollment_required,
        methods=methods,
        webauthn_challenge=webauthn_challenge,
        webauthn_rp_id=settings.webauthn_rp_id,
        webauthn_credential_id=(
            decrypt_secret(webauthn.credential_id)
            if webauthn is not None
            and webauthn.is_enrolled
            and webauthn.credential_id is not None
            else None
        ),
    )


async def _superadmin_challenge_context(
    db: AsyncSession, token: str
) -> tuple[dict[str, object], uuid.UUID]:
    claims = decode_challenge_token(token, role="superadmin")
    user_id = uuid.UUID(str(claims["sub"]))
    await _require_not_locked(db, user_id)
    if (
        not await iam_service.is_user_in_group(db, user_id, SUPERADMIN_GROUP)
        or await iam_service.is_user_in_group(db, user_id, ADMIN_GROUP)
        or await users_service.get_active_user(db, user_id) is None
    ):
        raise InvalidAuthTokenError
    active = await repository.get_superadmin_challenge(db, user_id)
    if active is None or active.active_jti != claims.get("jti"):
        raise InvalidAuthTokenError
    return claims, user_id


async def begin_superadmin_totp_enrollment(
    db: AsyncSession, *, challenge_token: str
) -> TotpEnrollmentRead:
    claims, user_id = await _superadmin_challenge_context(db, challenge_token)
    if claims.get("enrollment_required") is not True:
        raise MfaEnrollmentRequiredError
    credential = await repository.get_mfa_credential(
        db, user_id=user_id, mfa_type=MfaType.totp
    )
    secret = generate_totp_secret()
    fields = {
        "totp_secret": encrypt_secret(secret),
        "is_enrolled": False,
        "last_used_at": None,
    }
    if credential is None:
        repository.add_mfa_credential(
            db, user_id=user_id, mfa_type=MfaType.totp, **fields
        )
    else:
        repository.update_mfa_credential(db, credential, **fields)
    user = await users_service.get_active_user(db, user_id)
    audit_client.stage(
        db,
        action="superadmin.mfa_enrollment_started",
        actor_id=user_id,
        target_type="factor",
        target_id=str(user_id),
        prior_state={"enrolled": False},
        new_state={"enrolled": False},
    )
    await db.commit()
    return TotpEnrollmentRead(
        secret=secret, provisioning_uri=totp_provisioning_uri(secret, user.email)
    )


async def confirm_superadmin_totp_enrollment(
    db: AsyncSession,
    *,
    challenge_token: str,
    code: str,
    device_info: str,
    ip_address: str,
) -> SuperadminTokenPair:
    claims, user_id = await _superadmin_challenge_context(db, challenge_token)
    if claims.get("enrollment_required") is not True:
        raise MfaEnrollmentRequiredError
    credential = await repository.get_mfa_credential(
        db, user_id=user_id, mfa_type=MfaType.totp
    )
    if credential is None or credential.is_enrolled or credential.totp_secret is None:
        raise MfaEnrollmentRequiredError
    counter = matching_totp_counter(decrypt_secret(credential.totp_secret), code)
    if counter is None:
        await _record_failure(db, user_id, AuthFailureStep.mfa)
        raise InvalidMfaError
    repository.update_mfa_credential(
        db,
        credential,
        is_enrolled=True,
        last_used_at=datetime.fromtimestamp(counter * 30, UTC),
    )
    await repository.clear_superadmin_challenge(db, user_id)
    await _clear_failure_state(db, user_id)
    audit_client.stage(
        db,
        action="superadmin.mfa_enrollment_completed",
        actor_id=user_id,
        target_type="session",
        target_id=str(user_id),
        prior_state={"authenticated": False},
        new_state={"authenticated": True},
    )
    return await _issue_superadmin_pair(db, user_id, device_info, ip_address)


async def verify_superadmin_mfa(
    db: AsyncSession,
    *,
    challenge_token: str,
    code: str | None,
    assertion: dict[str, object] | None,
    device_info: str,
    ip_address: str,
) -> SuperadminTokenPair:
    claims, user_id = await _superadmin_challenge_context(db, challenge_token)
    if claims.get("enrollment_required") is True:
        raise MfaEnrollmentRequiredError
    if not await _verify_factor(db, user_id, claims, code, assertion):
        await _record_failure(db, user_id, AuthFailureStep.mfa)
        raise InvalidMfaError
    await repository.clear_superadmin_challenge(db, user_id)
    await _clear_failure_state(db, user_id)
    audit_client.stage(
        db,
        action="superadmin.login_completed",
        actor_id=user_id,
        target_type="session",
        target_id=str(user_id),
        prior_state={"authenticated": False},
        new_state={"authenticated": True},
    )
    return await _issue_superadmin_pair(db, user_id, device_info, ip_address)


async def _issue_superadmin_pair(
    db: AsyncSession, user_id: uuid.UUID, device_info: str, ip_address: str
) -> SuperadminTokenPair:
    session_id = uuid.uuid4()
    await auth_service.stage_superadmin_session(
        db,
        session_id=session_id,
        user_id=user_id,
        device_info=device_info[:1024],
        ip_address=ip_address,
    )
    pair = await auth_service.issue_session(
        db,
        user_id,
        claims={"role": "superadmin", "mfa": True, "sid": str(session_id)},
    )
    return SuperadminTokenPair(
        access_token=pair.access_token, refresh_token=pair.refresh_token
    )


async def refresh_superadmin(
    db: AsyncSession, request: RefreshRequest
) -> SuperadminTokenPair:
    claims = auth_client.decode_refresh_session(request.refresh_token)
    if claims.get("role") != "superadmin" or claims.get("mfa") is not True:
        raise InvalidAuthTokenError
    user_id = uuid.UUID(str(claims["sub"]))
    session_id = uuid.UUID(str(claims["sid"]))
    if not await iam_service.is_user_in_group(
        db, user_id, SUPERADMIN_GROUP
    ) or await iam_service.is_user_in_group(db, user_id, ADMIN_GROUP):
        raise InvalidAuthTokenError
    await auth_service.touch_superadmin_session(
        db, session_id=session_id, user_id=user_id
    )
    pair = await auth_service.refresh(
        db,
        request.refresh_token,
        session_claims={"role": "superadmin", "mfa": True, "sid": str(session_id)},
        commit=False,
    )
    audit_client.stage(
        db,
        action="superadmin.session_refreshed",
        actor_id=user_id,
        target_type="session",
        target_id=str(session_id),
    )
    await db.commit()
    return SuperadminTokenPair(
        access_token=pair.access_token, refresh_token=pair.refresh_token
    )


async def logout_superadmin(db: AsyncSession, request: RefreshRequest) -> None:
    claims = auth_client.decode_refresh_session(request.refresh_token)
    if claims.get("role") != "superadmin":
        raise InvalidAuthTokenError
    user_id = uuid.UUID(str(claims["sub"]))
    session_id = uuid.UUID(str(claims["sid"]))
    await auth_service.touch_superadmin_session(
        db, session_id=session_id, user_id=user_id
    )
    await auth_service.end_superadmin_session(
        db, session_id=session_id, user_id=user_id, commit=False
    )
    await auth_service.logout(
        db, request.refresh_token, allow_superadmin=True, commit=False
    )
    audit_client.stage(
        db,
        action="superadmin.session_ended",
        actor_id=user_id,
        target_type="session",
        target_id=str(session_id),
        prior_state={"active": True},
        new_state={"active": False},
    )
    await db.commit()


async def refresh_admin(db: AsyncSession, request: RefreshRequest) -> AdminTokenPair:
    """Rotate an Admin refresh token using the current role and profile scope."""
    refresh_claims = auth_client.decode_refresh_session(request.refresh_token)
    if refresh_claims.get("role") != "admin" or refresh_claims.get("mfa") is not True:
        raise InvalidAuthTokenError
    user_id = uuid.UUID(str(refresh_claims["sub"]))
    if not await iam_service.is_user_in_group(
        db, user_id, ADMIN_GROUP
    ) or await iam_service.is_user_in_group(db, user_id, SUPERADMIN_GROUP):
        raise InvalidAuthTokenError
    profile = await repository.get_admin_profile(db, user_id)
    if profile is None or not profile.is_active:
        raise AdminProfileRequiredError
    claims, position = _admin_session_claims(profile)
    pair = await auth_service.refresh(db, request.refresh_token, session_claims=claims)
    return AdminTokenPair(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        position=position,
        department_id=profile.department,
        college_id=profile.college,
    )


async def logout_admin(db: AsyncSession, request: RefreshRequest) -> None:
    """Revoke one Admin refresh token."""
    await auth_service.logout(db, request.refresh_token)


async def configure_admin_profile(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    position: AdminPosition,
    department_id: str | None = None,
    college_id: str | None = None,
):
    """Assign an Admin position and its valid data scope during bootstrap."""
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
            department=department_id,
            college=college_id,
        )
    else:
        repository.update_admin_profile(
            db,
            profile,
            position=position.value,
            department=department_id,
            college=college_id,
            is_active=True,
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
    """Persist output from a separately verified WebAuthn registration."""
    profile = await repository.get_admin_profile(db, user_id)
    if profile is None or not await iam_service.is_user_in_group(
        db, user_id, ADMIN_GROUP
    ):
        raise AdminProfileRequiredError
    credential = await repository.get_mfa_credential(
        db, user_id=user_id, mfa_type=MfaType.webauthn
    )
    fields = {
        "credential_id": encrypt_secret(credential_id),
        "credential_id_digest": credential_id_digest(credential_id),
        "public_key": encrypt_secret(credential_public_key),
        "sign_count": sign_count,
        "is_enrolled": True,
    }
    if credential is None:
        credential = repository.add_mfa_credential(
            db, user_id=user_id, mfa_type=MfaType.webauthn, **fields
        )
    else:
        repository.update_mfa_credential(db, credential, **fields)
    await db.commit()
    await db.refresh(credential)
    return credential


async def _factor_credentials(
    db: AsyncSession, user_id: uuid.UUID
) -> dict[MfaType, MfaCredential]:
    credentials = {}
    for mfa_type in MfaType:
        credential = await repository.get_mfa_credential(
            db, user_id=user_id, mfa_type=mfa_type
        )
        if credential is not None:
            credentials[mfa_type] = credential
    return credentials


async def _challenge_context(db: AsyncSession, token: str):
    claims = decode_challenge_token(token)
    user_id = uuid.UUID(str(claims["sub"]))
    await _require_not_locked(db, user_id)
    profile = await repository.get_admin_profile(db, user_id)
    if (
        profile is None
        or not profile.is_active
        or not await iam_service.is_user_in_group(db, user_id, ADMIN_GROUP)
    ):
        raise AdminProfileRequiredError
    if profile.active_mfa_challenge_jti != claims.get("jti"):
        raise InvalidAuthTokenError
    if await users_service.get_active_user(db, user_id) is None:
        raise InactiveUserError
    return claims, user_id, profile


async def _issue_admin_pair(
    db: AsyncSession, user_id: uuid.UUID, profile: AdminProfile
) -> AdminTokenPair:
    claims, position = _admin_session_claims(profile)
    pair = await auth_service.issue_session(db, user_id, claims=claims)
    return AdminTokenPair(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        position=position,
        department_id=profile.department,
        college_id=profile.college,
    )


def _admin_session_claims(
    profile: AdminProfile,
) -> tuple[dict[str, object], AdminPosition]:
    """Build claims only from a complete, currently valid Admin profile."""
    try:
        position = AdminPosition(profile.position)
    except ValueError as exc:
        raise AdminProfileRequiredError from exc
    if position is AdminPosition.chairperson and not profile.department:
        raise AdminProfileRequiredError
    if position is AdminPosition.dean and not profile.college:
        raise AdminProfileRequiredError
    return (
        {
            "role": "admin",
            "mfa": True,
            "position": position.value,
            "department_id": profile.department,
            "college_id": profile.college,
        },
        position,
    )


async def _require_not_locked(db: AsyncSession, user_id: uuid.UUID) -> None:
    await repository.acquire_authentication_lock(db, user_id)
    lockout = await repository.get_authentication_lockout(db, user_id)
    if lockout is None:
        return
    remaining = math.ceil(
        (_utc(lockout.locked_until) - datetime.now(UTC)).total_seconds()
    )
    if remaining > 0:
        raise AccountLockedError(remaining)
    await repository.clear_authentication_lockout(db, user_id=user_id)


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
    await repository.acquire_authentication_lock(db, user_id)
    await repository.clear_authentication_failures(db, user_id=user_id)
    await repository.clear_authentication_lockout(db, user_id=user_id)
