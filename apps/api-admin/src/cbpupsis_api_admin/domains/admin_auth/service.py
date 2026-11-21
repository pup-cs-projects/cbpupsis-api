"""Admin and Superadmin sign-in rules: password, second factor, scoped session.

The mechanics every role shares (lockout, challenges, TOTP, sessions) are in
``cbpupsis_shared.domains.auth`` and reached through its client. This module
keeps what is Admin-specific: group membership, the position profile and its
data scope, and hardware keys.
"""

from __future__ import annotations

import hmac
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.admin_auth.exceptions import AdminProfileRequiredError
from cbpupsis_api_admin.domains.admin_auth.schemas import (
    AdminLoginChallenge,
    AdminTokenPair,
    SuperadminEnrollmentRead,
    SuperadminLoginChallenge,
    SuperadminTokenPair,
    TotpEnrollmentRead,
    WebAuthnEnrollmentRead,
)
from cbpupsis_api_admin.domains.admin_auth.security import (
    verify_webauthn_assertion,
    verify_webauthn_registration,
    webauthn_registration_options,
)
from cbpupsis_core.config import settings
from cbpupsis_shared.domains.audit import client as audit_client
from cbpupsis_shared.domains.auth import client as auth_client
from cbpupsis_shared.domains.auth.client import AuthFailureStep, MfaType
from cbpupsis_shared.domains.auth.constants import ROLE_ADMIN, ROLE_SUPERADMIN
from cbpupsis_shared.domains.auth.exceptions import (
    InactiveUserError,
    InvalidAuthTokenError,
    InvalidCredentialsError,
    InvalidMfaError,
    MfaEnrollmentRequiredError,
    UnverifiedEmailError,
)
from cbpupsis_shared.domains.auth.schemas import RefreshRequest
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP, SUPERADMIN_GROUP
from cbpupsis_shared.domains.users import client as users_client
from cbpupsis_shared.domains.users.constants import AdminPosition
from cbpupsis_shared.domains.users.schemas import AdminProfileRead


async def _password_step(db: AsyncSession, email: str, password: str):
    """Verify an email and password under the shared lockout, for both roles.

    Every identifier locks alike (an unknown email through its keyed
    pseudonym), and the lock is checked before the password, so neither the
    response nor its timing tells an attacker which emails are accounts.
    """
    subject = await users_client.get_email_sign_in(db, email)
    subject_id = (
        subject.user_id if subject is not None else auth_client.lockout_subject(email)
    )
    await auth_client.require_not_locked(db, subject_id)
    valid = await auth_client.check_password(
        password, subject.password_hash if subject is not None else None
    )
    if subject is None or not valid or not subject.is_active:
        await auth_client.record_sign_in_failure(
            db, subject_id, AuthFailureStep.credentials, notify=subject is not None
        )
        raise InvalidCredentialsError
    await auth_client.refuse_expired_temporary_password(
        db,
        user_id=subject.user_id,
        password_change_required=subject.password_change_required,
        expires_at=subject.temporary_password_expires_at,
        attempted_id=email,
        ip_address=None,
        device_info="",
    )
    return subject


async def login_admin(
    db: AsyncSession, *, email: str, password: str
) -> AdminLoginChallenge:
    """Verify an Admin password and return only a non-privileged challenge."""
    subject = await _password_step(db, email, password)
    if not await iam_service.is_user_in_group(
        db, subject.user_id, ADMIN_GROUP
    ) or await iam_service.is_user_in_group(db, subject.user_id, SUPERADMIN_GROUP):
        raise InvalidCredentialsError
    if not subject.email_verified:
        raise UnverifiedEmailError("Verify your email address to continue")
    profile = await users_client.get_admin_profile(db, subject.user_id)
    if profile is None or not profile.is_active:
        raise AdminProfileRequiredError

    enrolled = await auth_client.enrolled_factors(db, subject.user_id)
    token, webauthn_challenge, jti = auth_client.create_challenge_token(
        subject.user_id,
        role=ROLE_ADMIN,
        enrollment_required=not enrolled,
        session_version=subject.session_version,
    )
    await auth_client.bind_mfa_challenge(db, subject.user_id, jti)
    webauthn = await auth_client.get_webauthn_factor(db, subject.user_id)
    await db.commit()
    return AdminLoginChallenge(
        challenge_token=token,
        enrollment_required=not enrolled,
        methods=[factor.value for factor in enrolled],
        webauthn_challenge=webauthn_challenge,
        webauthn_rp_id=settings.webauthn_rp_id,
        webauthn_credential_id=webauthn.credential_id if webauthn else None,
    )


async def begin_totp_enrollment(
    db: AsyncSession, *, challenge_token: str
) -> TotpEnrollmentRead:
    """Create an encrypted pending seed for Google Authenticator enrollment."""
    challenge = await _open_challenge(db, challenge_token)
    claims, user_id = challenge.claims, challenge.user_id
    if claims.get("enrollment_required") is not True:
        raise MfaEnrollmentRequiredError
    user = await users_client.get_active_user(db, user_id)
    if user is None:
        raise InactiveUserError
    enrollment = await auth_client.begin_totp_enrollment(db, user_id, user.email)
    await db.commit()
    return TotpEnrollmentRead(
        secret=enrollment.secret, provisioning_uri=enrollment.provisioning_uri
    )


async def confirm_totp_enrollment(
    db: AsyncSession,
    *,
    challenge_token: str,
    code: str,
    ip_address: str | None,
    device_info: str,
) -> AdminTokenPair:
    """Prove the pending seed and issue the first Admin session."""
    challenge = await _open_challenge(db, challenge_token)
    claims, user_id = challenge.claims, challenge.user_id
    if claims.get("enrollment_required") is not True:
        raise MfaEnrollmentRequiredError
    await auth_client.verify_totp(db, user_id, code, enrolling=True)
    return await _complete_sign_in(db, challenge, ip_address, device_info)


async def begin_webauthn_enrollment(
    db: AsyncSession, *, challenge_token: str
) -> WebAuthnEnrollmentRead:
    """Return hardware-key options bound to an unenrolled Admin challenge."""
    challenge = await _open_challenge(db, challenge_token)
    claims, user_id = challenge.claims, challenge.user_id
    if claims.get("enrollment_required") is not True:
        raise MfaEnrollmentRequiredError
    user = await users_client.get_active_user(db, user_id)
    if user is None:
        raise InactiveUserError
    return WebAuthnEnrollmentRead(
        public_key=webauthn_registration_options(
            user_id=user_id, email=user.email, challenge=str(claims["challenge"])
        )
    )


async def confirm_webauthn_enrollment(
    db: AsyncSession,
    *,
    challenge_token: str,
    credential: dict[str, Any],
    ip_address: str | None,
    device_info: str,
) -> AdminTokenPair:
    """Verify the registration ceremony and issue the first Admin session."""
    challenge = await _open_challenge(db, challenge_token)
    claims, user_id = challenge.claims, challenge.user_id
    if claims.get("enrollment_required") is not True:
        raise MfaEnrollmentRequiredError
    verified = verify_webauthn_registration(
        credential=credential, challenge=str(claims["challenge"])
    )
    if verified is None:
        await auth_client.record_sign_in_failure(db, user_id, AuthFailureStep.mfa)
        raise InvalidMfaError
    credential_id, public_key, sign_count = verified
    owner = await auth_client.webauthn_credential_owner(db, credential_id)
    if owner is not None and owner != user_id:
        await auth_client.record_sign_in_failure(db, user_id, AuthFailureStep.mfa)
        raise InvalidMfaError
    await auth_client.stage_webauthn_factor(
        db,
        user_id,
        credential_id=credential_id,
        public_key=public_key,
        sign_count=sign_count,
    )
    try:
        return await _complete_sign_in(db, challenge, ip_address, device_info)
    except IntegrityError as exc:
        # A concurrent registration of the same key won the unique index.
        await db.rollback()
        await auth_client.record_sign_in_failure(db, user_id, AuthFailureStep.mfa)
        raise InvalidMfaError from exc


async def verify_mfa(
    db: AsyncSession,
    *,
    challenge_token: str,
    code: str | None,
    assertion: dict[str, object] | None,
    ip_address: str | None,
    device_info: str,
) -> AdminTokenPair:
    """Complete a challenge using TOTP or a verified hardware-key assertion."""
    challenge = await _open_challenge(db, challenge_token)
    claims, user_id = challenge.claims, challenge.user_id
    if claims.get("enrollment_required") is True:
        raise MfaEnrollmentRequiredError
    if code is not None:
        await auth_client.verify_totp(db, user_id, code, enrolling=False)
    else:
        await _verify_assertion(db, user_id, claims, assertion or {})
    return await _complete_sign_in(db, challenge, ip_address, device_info)


async def _verify_assertion(
    db: AsyncSession,
    user_id: uuid.UUID,
    claims: dict[str, Any],
    assertion: dict[str, object],
) -> None:
    factor = await auth_client.get_webauthn_factor(db, user_id)
    assertion_id = assertion.get("id")
    new_count = None
    if (
        factor is not None
        and isinstance(assertion_id, str)
        and hmac.compare_digest(assertion_id, factor.credential_id)
    ):
        new_count = verify_webauthn_assertion(
            assertion=assertion,
            challenge=str(claims["challenge"]),
            credential_public_key=factor.public_key,
            current_sign_count=factor.sign_count,
        )
    if new_count is None:
        await auth_client.record_sign_in_failure(db, user_id, AuthFailureStep.mfa)
        raise InvalidMfaError
    await auth_client.record_webauthn_use(db, user_id, new_count)


async def refresh_admin(db: AsyncSession, request: RefreshRequest) -> AdminTokenPair:
    """Rotate an Admin refresh token using the current role and profile scope."""
    refresh_claims = auth_client.decode_refresh_session(request.refresh_token)
    if (
        refresh_claims.get("role") != ROLE_ADMIN
        or refresh_claims.get("mfa") is not True
    ):
        raise InvalidAuthTokenError
    user_id = uuid.UUID(str(refresh_claims["sub"]))
    if await iam_service.is_user_in_group(db, user_id, SUPERADMIN_GROUP):
        raise InvalidAuthTokenError
    if not await iam_service.is_user_in_group(db, user_id, ADMIN_GROUP):
        raise InvalidAuthTokenError
    profile = await users_client.get_admin_profile(db, user_id)
    if profile is None or not profile.is_active:
        raise AdminProfileRequiredError
    claims, position = _admin_session_claims(profile)
    pair = await auth_client.refresh_role_session(
        db, request.refresh_token, session_claims=claims
    )
    return _admin_pair(pair.access_token, pair.refresh_token, position, profile)


async def logout_admin(db: AsyncSession, request: RefreshRequest) -> None:
    """End one Admin session and revoke its refresh token."""
    await auth_client.revoke_refresh_token(db, request.refresh_token)


async def configure_admin_profile(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    position: AdminPosition,
    department_id: str | None = None,
    college_id: str | None = None,
) -> AdminProfileRead:
    """Assign an Admin position and its valid data scope during bootstrap."""
    if not await iam_service.is_user_in_group(db, user_id, ADMIN_GROUP):
        raise AdminProfileRequiredError
    if position is AdminPosition.chairperson and not department_id:
        raise ValueError("chairperson requires department_id")
    if position is AdminPosition.dean and not college_id:
        raise ValueError("dean requires college_id")
    department_id, college_id = users_client.admin_scope(
        position, department_id, college_id
    )
    return await users_client.save_admin_profile(
        db,
        user_id=user_id,
        position=position,
        department=department_id,
        college=college_id,
    )


async def _admin_profile(db: AsyncSession, user_id: uuid.UUID) -> AdminProfileRead:
    if await iam_service.is_user_in_group(db, user_id, SUPERADMIN_GROUP):
        raise InvalidAuthTokenError
    profile = await users_client.get_admin_profile(db, user_id)
    if (
        profile is None
        or not profile.is_active
        or not await iam_service.is_user_in_group(db, user_id, ADMIN_GROUP)
    ):
        raise AdminProfileRequiredError
    return profile


async def _open_challenge(
    db: AsyncSession, token: str
) -> auth_client.Challenge[AdminProfileRead]:
    return await auth_client.open_challenge(
        db, token, role=ROLE_ADMIN, load_profile=_admin_profile
    )


async def _complete_sign_in(
    db: AsyncSession,
    challenge: auth_client.Challenge[AdminProfileRead],
    ip_address: str | None,
    device_info: str,
) -> AdminTokenPair:
    claims, position = _admin_session_claims(challenge.profile)
    pair = await auth_client.complete_sign_in(
        db,
        challenge.user_id,
        role=ROLE_ADMIN,
        attempted_id=str(challenge.user_id),
        ip_address=ip_address,
        device_info=device_info,
        mfa=True,
        claims=claims,
    )
    return _admin_pair(
        pair.access_token, pair.refresh_token, position, challenge.profile
    )


def _admin_pair(
    access_token: str,
    refresh_token: str,
    position: AdminPosition,
    profile: AdminProfileRead,
) -> AdminTokenPair:
    return AdminTokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        position=position,
        department_id=profile.department,
        college_id=profile.college,
    )


def _admin_session_claims(
    profile: AdminProfileRead,
) -> tuple[dict[str, object], AdminPosition]:
    """Build scope claims only from a complete, currently valid Admin profile.

    The claims are a cache for the client: ``require_admin_session`` compares
    them with the live profile on every request and refuses a mismatch.
    """
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
            "role": ROLE_ADMIN,
            "mfa": True,
            "position": position.value,
            "department_id": profile.department,
            "college_id": profile.college,
        },
        position,
    )


async def login_superadmin(
    db: AsyncSession, *, email: str, password: str
) -> SuperadminLoginChallenge:
    """Verify a Superadmin password and return only a challenge."""
    subject = await _password_step(db, email, password)
    await auth_client.require_superadmin_actor(db, subject.user_id)
    enrolled = MfaType.totp in await auth_client.enrolled_factors(db, subject.user_id)
    token, _challenge, jti = auth_client.create_challenge_token(
        subject.user_id,
        role=ROLE_SUPERADMIN,
        enrollment_required=not enrolled,
        session_version=subject.session_version,
    )
    await auth_client.bind_mfa_challenge(db, subject.user_id, jti)
    await db.commit()
    return SuperadminLoginChallenge(
        challenge_token=token,
        enrollment_required=not enrolled,
        methods=["totp"] if enrolled else [],
    )


async def _superadmin_actor(db: AsyncSession, user_id: uuid.UUID) -> None:
    await auth_client.require_superadmin_actor(db, user_id)


async def _open_superadmin_challenge(
    db: AsyncSession, token: str
) -> auth_client.Challenge[None]:
    return await auth_client.open_challenge(
        db, token, role=ROLE_SUPERADMIN, load_profile=_superadmin_actor
    )


async def enroll_superadmin_totp(
    db: AsyncSession, *, challenge_token: str
) -> SuperadminEnrollmentRead:
    challenge = await _open_superadmin_challenge(db, challenge_token)
    claims, user_id = challenge.claims, challenge.user_id
    if claims.get("enrollment_required") is not True:
        raise MfaEnrollmentRequiredError
    user = await users_client.get_active_user(db, user_id)
    if user is None:
        raise InactiveUserError
    enrollment = await auth_client.begin_totp_enrollment(db, user_id, user.email)
    await db.commit()
    return SuperadminEnrollmentRead(provisioning_uri=enrollment.provisioning_uri)


async def verify_superadmin_totp(
    db: AsyncSession,
    *,
    challenge_token: str,
    code: str,
    enrollment: bool,
    ip_address: str | None,
    device_info: str,
) -> SuperadminTokenPair:
    challenge = await _open_superadmin_challenge(db, challenge_token)
    claims, user_id = challenge.claims, challenge.user_id
    if claims.get("enrollment_required") is not enrollment:
        raise MfaEnrollmentRequiredError
    await auth_client.verify_totp(db, user_id, code, enrolling=enrollment)
    pair = await auth_client.complete_sign_in(
        db,
        user_id,
        role=ROLE_SUPERADMIN,
        attempted_id=str(user_id),
        ip_address=ip_address,
        device_info=device_info,
        mfa=True,
        commit=False,
    )
    sid = auth_client.decode_access_session(pair.access_token)["sid"]
    audit_client.stage(
        db,
        action="superadmin.login",
        actor_id=user_id,
        target_type="session",
        target_id=sid,
        prior_state={},
        new_state={"active": True},
    )
    await db.commit()
    return SuperadminTokenPair(**pair.model_dump())


async def refresh_superadmin(
    db: AsyncSession, request: RefreshRequest
) -> SuperadminTokenPair:
    claims = auth_client.decode_refresh_session(request.refresh_token)
    # Refreshing only keeps the session alive; what it mints is still guarded.
    sid = await auth_client.check_superadmin_session(
        db, claims, allow_password_change=True
    )
    user_id = uuid.UUID(str(claims["sub"]))
    pair = await auth_client.refresh_role_session(
        db,
        request.refresh_token,
        session_claims={"role": ROLE_SUPERADMIN, "mfa": True, "sid": str(sid)},
        commit=False,
    )
    audit_client.stage(
        db,
        action="superadmin.refresh",
        actor_id=user_id,
        target_type="session",
        target_id=sid,
        prior_state={"active": True},
        new_state={"active": True},
    )
    await db.commit()
    return SuperadminTokenPair(**pair.model_dump())


async def logout_superadmin(db: AsyncSession, request: RefreshRequest) -> None:
    claims = auth_client.decode_refresh_session(request.refresh_token)
    if claims.get("role") != ROLE_SUPERADMIN:
        raise InvalidAuthTokenError
    await auth_client.revoke_refresh_token(db, request.refresh_token)
