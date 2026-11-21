"""Faculty sign-in rules (US-102): employee id and password, then TOTP.

A password alone never yields a faculty session: it yields a challenge token
that every protected route refuses, and only a verified second factor (a TOTP
code, or a single-use recovery code) turns that into a session. The shared
mechanics (lockout, ledger, challenge, session) come from the auth client's
role sign-in; the faculty profile comes from the users client.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_faculty.domains.faculty_auth.schemas import (
    FacultyEnrollmentResult,
    FacultyLoginChallenge,
    FacultyMfaVerifyRequest,
    FacultyTokenPair,
    TotpEnrollmentRead,
)
from cbpupsis_shared.domains.auth import client as auth_client
from cbpupsis_shared.domains.auth.client import MfaType
from cbpupsis_shared.domains.auth.constants import (
    EMPLOYEE_ID_FORMAT_MESSAGE,
    FACULTY_SIGN_IN_FAILED_MESSAGE,
    INSTITUTIONAL_ID_PATTERN,
    ROLE_FACULTY,
)
from cbpupsis_shared.domains.auth.exceptions import (
    IdentifierFormatInvalidError,
    IdentifierSignInFailedError,
    InsufficientRoleError,
    InvalidAuthTokenError,
    MfaEnrollmentRequiredError,
)
from cbpupsis_shared.domains.auth.schemas import RefreshRequest
from cbpupsis_shared.domains.users import client as users_client
from cbpupsis_shared.domains.users.schemas import FacultyProfileRead


async def login_faculty(
    db: AsyncSession,
    *,
    employee_id: str,
    password: str,
    ip_address: str | None,
    device_info: str,
) -> FacultyLoginChallenge:
    """Verify an employee id and password; return only an MFA challenge.

    Unknown id, wrong password, and an inactive account or faculty profile all
    fail identically, and the 5th failure in the window locks the id (423)
    whether or not it belongs to an account.
    """
    if not INSTITUTIONAL_ID_PATTERN.match(employee_id):
        raise IdentifierFormatInvalidError(EMPLOYEE_ID_FORMAT_MESSAGE)

    subject = await users_client.get_faculty_sign_in(db, employee_id)

    async def reject(subject_id: uuid.UUID, password_ok: bool) -> str | None:
        profile = await users_client.get_faculty_profile(db, subject_id)
        if (
            subject is None
            or not subject.is_active
            or profile is None
            or not profile.is_active
            or not password_ok
        ):
            return "AUTH_FAILED"
        return None

    subject_id = await auth_client.verify_identifier_password(
        db,
        identifier=employee_id,
        subject=subject,
        password=password,
        ip_address=ip_address,
        device_info=device_info,
        reject=reject,
        failure=IdentifierSignInFailedError(FACULTY_SIGN_IN_FAILED_MESSAGE),
    )
    # The password step is its own ledger row; the MFA completion adds a second.
    auth_client.record_attempt(
        db,
        attempted_id=employee_id,
        user_id=subject_id,
        ip_address=ip_address,
        device_info=device_info,
    )
    enrolled = MfaType.totp in await auth_client.enrolled_factors(db, subject_id)
    token, _challenge, jti = auth_client.create_challenge_token(
        subject_id,
        role=ROLE_FACULTY,
        enrollment_required=not enrolled,
        session_version=subject.session_version,
    )
    await auth_client.bind_mfa_challenge(db, subject_id, jti)
    await db.commit()
    return FacultyLoginChallenge(
        challenge_token=token,
        enrollment_required=not enrolled,
        methods=["totp"] if enrolled else [],
    )


async def begin_totp_enrollment(
    db: AsyncSession, *, challenge_token: str
) -> TotpEnrollmentRead:
    """Stage a pending authenticator seed for a faculty member not yet enrolled.

    Refused once TOTP is enrolled, so the password alone can never replace the
    authenticator that protects the account.
    """
    challenge = await _open_challenge(db, challenge_token)
    if challenge.claims.get("enrollment_required") is not True:
        raise MfaEnrollmentRequiredError
    enrollment = await auth_client.begin_totp_enrollment(
        db, challenge.user_id, _attempted_id(challenge)
    )
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
) -> FacultyEnrollmentResult:
    """Prove the pending seed, issue recovery codes, and open the first session."""
    challenge = await _open_challenge(db, challenge_token)
    if challenge.claims.get("enrollment_required") is not True:
        raise MfaEnrollmentRequiredError
    await auth_client.verify_totp(db, challenge.user_id, code, enrolling=True)
    recovery_codes = await auth_client.issue_recovery_codes(db, challenge.user_id)
    pair = await _complete_sign_in(db, challenge, ip_address, device_info)
    return FacultyEnrollmentResult(**pair.model_dump(), recovery_codes=recovery_codes)


async def verify_mfa(
    db: AsyncSession,
    data: FacultyMfaVerifyRequest,
    *,
    ip_address: str | None,
    device_info: str,
) -> FacultyTokenPair:
    """Complete sign-in with a TOTP code or one unused recovery code."""
    challenge = await _open_challenge(db, data.challenge_token)
    if challenge.claims.get("enrollment_required") is True:
        raise MfaEnrollmentRequiredError
    if data.code is not None:
        await auth_client.verify_totp(db, challenge.user_id, data.code, enrolling=False)
    else:
        await auth_client.redeem_recovery_code(
            db, challenge.user_id, data.recovery_code or ""
        )
    return await _complete_sign_in(db, challenge, ip_address, device_info)


async def refresh_faculty(
    db: AsyncSession, request: RefreshRequest
) -> FacultyTokenPair:
    """Rotate a faculty session's refresh token after re-checking the profile."""
    claims = auth_client.decode_refresh_session(request.refresh_token)
    if claims.get("role") != ROLE_FACULTY or claims.get("mfa") is not True:
        raise InvalidAuthTokenError
    await _active_profile(db, uuid.UUID(str(claims["sub"])))
    pair = await auth_client.refresh_role_session(
        db,
        request.refresh_token,
        session_claims={"role": ROLE_FACULTY, "mfa": True},
    )
    return auth_client.expiring_pair(FacultyTokenPair, pair)


async def logout_faculty(db: AsyncSession, request: RefreshRequest) -> None:
    """End the faculty session and revoke its refresh token. Idempotent."""
    await auth_client.revoke_refresh_token(db, request.refresh_token)


async def _active_profile(db: AsyncSession, user_id: uuid.UUID) -> FacultyProfileRead:
    profile = await users_client.get_faculty_profile(db, user_id)
    if profile is None or not profile.is_active:
        raise InsufficientRoleError
    return profile


async def _open_challenge(
    db: AsyncSession, token: str
) -> auth_client.Challenge[FacultyProfileRead]:
    return await auth_client.open_challenge(
        db, token, role=ROLE_FACULTY, load_profile=_active_profile
    )


def _attempted_id(challenge: auth_client.Challenge[FacultyProfileRead]) -> str:
    return challenge.profile.employee_id or str(challenge.user_id)


async def _complete_sign_in(
    db: AsyncSession,
    challenge: auth_client.Challenge[FacultyProfileRead],
    ip_address: str | None,
    device_info: str,
) -> FacultyTokenPair:
    pair = await auth_client.complete_sign_in(
        db,
        challenge.user_id,
        role=ROLE_FACULTY,
        attempted_id=_attempted_id(challenge),
        ip_address=ip_address,
        device_info=device_info,
        mfa=True,
    )
    return auth_client.expiring_pair(FacultyTokenPair, pair)
