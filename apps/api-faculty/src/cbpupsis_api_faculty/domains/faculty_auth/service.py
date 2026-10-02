"""Application services for the faculty authentication and MFA domain."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_faculty.domains.faculty_auth import repository
from cbpupsis_api_faculty.domains.faculty_auth.exceptions import (
    AuditImmutableError,
    DatabaseServiceUnavailableError,
    FacultyAccountLockedError,
    FacultyAuthFailedError,
    FacultyMfaCodeReusedError,
    FacultyMfaExpiredError,
    FacultyMfaInvalidError,
    InvalidIdentifierFormatError,
    MfaEnrollmentRequiredError,
    ResourceNotFoundError,
)
from cbpupsis_api_faculty.domains.faculty_auth.schemas import (
    FacultyLoginChallengeData,
    FacultyMfaEnrollData,
    FacultySessionData,
    FacultyUserData,
    SectionItem,
)
from cbpupsis_api_faculty.domains.faculty_auth.security import (
    create_faculty_access_token,
    create_faculty_challenge_token,
    create_faculty_refresh_token,
    decode_faculty_challenge_token,
    decrypt_totp_secret,
    encrypt_totp_secret,
    generate_recovery_codes,
    generate_totp_secret,
    hash_recovery_code,
    is_recently_expired_totp,
    matching_totp_counter,
    totp_provisioning_uri,
)
from cbpupsis_api_faculty.domains.faculty_auth.validation import (
    validate_faculty_identifier,
)
from cbpupsis_core.emails.sender import send_email
from cbpupsis_shared.domains.auth import client as auth_client

logger = logging.getLogger(__name__)

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_WINDOW_MINUTES = 15
LOCKOUT_DURATION_SECONDS = 900


async def _check_and_enforce_lockout(
    db: AsyncSession,
    attempted_id: str,
    user_id: uuid.UUID | None = None,
    institutional_email: str | None = None,
) -> None:
    """Check failed attempts in rolling 15-minute window and enforce lockout."""
    since = datetime.now(UTC) - timedelta(minutes=LOCKOUT_WINDOW_MINUTES)
    failures = await repository.get_recent_failed_attempts(
        db, attempted_id, since, user_id=user_id
    )
    if len(failures) >= MAX_FAILED_ATTEMPTS:
        if institutional_email:
            try:
                await send_email(
                    to=institutional_email,
                    subject="Security Notice: PUP Faculty Account Locked",
                    body=(
                        "Your faculty account has been locked for 15 minutes "
                        "due to 5 consecutive failed login attempts."
                    ),
                )
            except Exception as exc:
                logger.warning("Could not dispatch lockout notification email: %s", exc)
        raise FacultyAccountLockedError(
            retry_after_seconds=LOCKOUT_DURATION_SECONDS,
            code="AUTH_ACCOUNT_LOCKED",
        )


async def login_faculty(
    db: AsyncSession,
    *,
    identifier: str | None = None,
    password: str,
    birthday: str | None = None,
    email: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> FacultyLoginChallengeData:
    """Execute faculty password authentication and issue an MFA challenge.

    Implements AC-002.1 and AC-002.6.
    """
    attempted_id = identifier or email or ""
    if identifier and not validate_faculty_identifier(identifier):
        raise InvalidIdentifierFormatError

    await _check_and_enforce_lockout(db, attempted_id)

    faculty_record = None
    if identifier:
        faculty_record = await repository.get_faculty_by_employee_id(db, identifier)

    if faculty_record is None and email:
        pass

    if faculty_record is None:
        await repository.record_audit_log(
            db,
            attempted_id=attempted_id,
            user_id=None,
            ip_address=ip_address,
            user_agent=user_agent,
            success=False,
            failure_reason="AUTH_FAILED",
            mfa_used=False,
        )
        since = datetime.now(UTC) - timedelta(minutes=LOCKOUT_WINDOW_MINUTES)
        recent = await repository.get_recent_failed_attempts(db, attempted_id, since)
        if len(recent) >= MAX_FAILED_ATTEMPTS:
            raise FacultyAccountLockedError(
                retry_after_seconds=LOCKOUT_DURATION_SECONDS,
                code="AUTH_ACCOUNT_LOCKED",
            )
        remaining = max(0, MAX_FAILED_ATTEMPTS - len(recent))
        raise FacultyAuthFailedError(remaining_attempts=remaining)

    user, faculty_profile, _user_profile = faculty_record

    if not user.is_active or not auth_client.check_password(
        password, user.password_hash
    ):
        await repository.record_audit_log(
            db,
            attempted_id=attempted_id,
            user_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
            success=False,
            failure_reason="AUTH_FAILED",
            mfa_used=False,
        )
        since = datetime.now(UTC) - timedelta(minutes=LOCKOUT_WINDOW_MINUTES)
        recent = await repository.get_recent_failed_attempts(
            db, attempted_id, since, user_id=user.id
        )
        if len(recent) >= MAX_FAILED_ATTEMPTS:
            try:
                await send_email(
                    to=user.email,
                    subject="Security Notice: PUP Faculty Account Locked",
                    body=(
                        "Your faculty account has been locked due to 5 consecutive "
                        "failed login attempts."
                    ),
                )
            except Exception as exc:
                logger.warning("Could not dispatch lockout email: %s", exc)
        remaining = max(0, MAX_FAILED_ATTEMPTS - len(recent))
        raise FacultyAuthFailedError(remaining_attempts=remaining)

    creds = await repository.get_user_mfa_credentials(db, user.id)
    is_mfa_enrolled = any(c.is_enrolled for c in creds if c.mfa_type == "totp")

    await repository.record_audit_log(
        db,
        attempted_id=attempted_id,
        user_id=user.id,
        ip_address=ip_address,
        user_agent=user_agent,
        success=True,
        failure_reason=None,
        mfa_used=False,
    )

    employee_id = faculty_profile.employee_id or attempted_id
    challenge_token = create_faculty_challenge_token(
        user_id=user.id, identifier=employee_id
    )

    return FacultyLoginChallengeData(
        challenge_token=challenge_token,
        mfa_required=True,
        expires_in=300,
        mfa_enrolled=is_mfa_enrolled,
    )


async def verify_faculty_mfa(
    db: AsyncSession,
    *,
    challenge_token: str,
    code: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[FacultySessionData, str]:
    """Verify second-factor TOTP code and issue a faculty session.

    Implements AC-002.2, AC-002.3, AC-002.4, and AC-002.5.
    Returns (session_data, refresh_token).
    """
    try:
        claims = decode_faculty_challenge_token(challenge_token)
    except Exception as exc:
        raise FacultyMfaInvalidError(
            detail="Invalid or expired challenge token."
        ) from exc

    user_id = uuid.UUID(claims["sub"])
    identifier = str(claims.get("identifier", ""))
    jti = str(claims.get("jti", ""))

    try:
        if await repository.is_challenge_spent(db, jti):
            raise FacultyMfaInvalidError(
                detail="Challenge token has already been consumed."
            )

        record = await repository.get_faculty_by_user_id(db, user_id)
        if record is None:
            raise FacultyMfaInvalidError

        user, faculty_profile, user_profile = record
        employee_id = faculty_profile.employee_id or identifier
        if not employee_id:
            raise FacultyMfaInvalidError

        await _check_and_enforce_lockout(
            db, identifier, user_id=user.id, institutional_email=user.email
        )

        creds = await repository.get_user_mfa_credentials(db, user.id)
        totp_cred = next(
            (c for c in creds if c.mfa_type == "totp" and c.is_enrolled), None
        )
        if totp_cred is None or not totp_cred.totp_secret:
            await repository.record_audit_log(
                db,
                attempted_id=identifier,
                user_id=user.id,
                ip_address=ip_address,
                user_agent=user_agent,
                success=False,
                failure_reason="AUTH_MFA_ENROLLMENT_REQUIRED",
                mfa_used=True,
            )
            raise MfaEnrollmentRequiredError

        plain_secret = decrypt_totp_secret(totp_cred.totp_secret)
        matched_counter = matching_totp_counter(plain_secret, code, drift_steps=1)

        if matched_counter is None:
            await repository.record_audit_log(
                db,
                attempted_id=identifier,
                user_id=user.id,
                ip_address=ip_address,
                user_agent=user_agent,
                success=False,
                failure_reason="AUTH_MFA_INVALID",
                mfa_used=True,
            )
            since = datetime.now(UTC) - timedelta(minutes=LOCKOUT_WINDOW_MINUTES)
            recent = await repository.get_recent_failed_attempts(
                db, identifier, since, user_id=user.id
            )
            if len(recent) >= MAX_FAILED_ATTEMPTS:
                try:
                    await send_email(
                        to=user.email,
                        subject="Security Notice: PUP Faculty Account Locked",
                        body=(
                            "Your faculty account has been locked due to 5 consecutive "
                            "failed attempts."
                        ),
                    )
                except Exception as exc:
                    logger.warning("Could not dispatch lockout email: %s", exc)

            if is_recently_expired_totp(plain_secret, code):
                raise FacultyMfaExpiredError

            remaining = max(0, MAX_FAILED_ATTEMPTS - len(recent))
            raise FacultyMfaInvalidError(remaining_attempts=remaining)

        is_reused = await repository.is_totp_code_spent(
            db, user_id=user.id, code=code, counter=matched_counter
        )
        if is_reused or (
            totp_cred.sign_count and totp_cred.sign_count >= matched_counter
        ):
            await repository.record_audit_log(
                db,
                attempted_id=identifier,
                user_id=user.id,
                ip_address=ip_address,
                user_agent=user_agent,
                success=False,
                failure_reason="AUTH_MFA_CODE_REUSED",
                mfa_used=True,
            )
            raise FacultyMfaCodeReusedError

        now = datetime.now(UTC)
        await repository.mark_challenge_spent(
            db, jti=jti, user_id=user.id, expires_at=now + timedelta(minutes=5)
        )
        await repository.mark_totp_code_spent(
            db,
            user_id=user.id,
            code=code,
            counter=matched_counter,
            expires_at=now + timedelta(seconds=90),
        )

        totp_cred.sign_count = matched_counter
        totp_cred.last_used_at = now
        await db.flush()

        await repository.record_audit_log(
            db,
            attempted_id=identifier,
            user_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
            success=True,
            failure_reason=None,
            mfa_used=True,
        )

        access_token = create_faculty_access_token(
            user_id=user.id, identifier=employee_id
        )
        refresh_token, _, _ = create_faculty_refresh_token(user_id=user.id)

        session_hash = str(uuid.uuid5(uuid.NAMESPACE_DNS, access_token))[:64]
        await repository.create_active_session(
            db,
            user_id=user.id,
            session_token_hash=session_hash,
            device_info=user_agent or "Unknown Device",
            ip_address=ip_address or "127.0.0.1",
        )

        first_name = user_profile.first_name if user_profile else "Faculty"
        last_name = user_profile.last_name if user_profile else "Member"

        session_data = FacultySessionData(
            accessToken=access_token,
            tokenType="Bearer",
            expiresIn=1800,
            role="faculty",
            user=FacultyUserData(
                id=employee_id,
                firstName=first_name,
                lastName=last_name,
                email=user.email,
                role="FACULTY",
            ),
        )
        return session_data, refresh_token

    except (
        FacultyAccountLockedError,
        FacultyMfaInvalidError,
        FacultyMfaCodeReusedError,
        FacultyMfaExpiredError,
        MfaEnrollmentRequiredError,
    ):
        raise
    except SQLAlchemyError as exc:
        logger.error("Database failure during MFA verification: %s", exc)
        raise DatabaseServiceUnavailableError from exc


async def enroll_faculty_mfa(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> FacultyMfaEnrollData:
    """Initiate Google Authenticator TOTP enrollment for a faculty account."""
    record = await repository.get_faculty_by_user_id(db, user_id)
    if record is None:
        raise ResourceNotFoundError("Faculty user not found.")

    user, faculty_profile, _ = record
    raw_secret = generate_totp_secret()
    encrypted_secret = encrypt_totp_secret(raw_secret)

    await repository.create_or_update_mfa_credential(
        db,
        user_id=user.id,
        totp_secret=encrypted_secret,
        is_enrolled=False,
    )

    account_name = faculty_profile.employee_id or user.email
    uri = totp_provisioning_uri(
        secret=raw_secret,
        account_name=account_name,
        issuer="PUP CBPUPSIS",
    )
    return FacultyMfaEnrollData(
        provisioning_uri=uri,
        account_name=account_name,
        issuer="PUP CBPUPSIS",
        mfa_enrolled=False,
    )


async def confirm_faculty_mfa_enrollment(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    code: str,
) -> list[str]:
    """Verify submitted code, activate enrollment, and issue recovery backup codes."""
    creds = await repository.get_user_mfa_credentials(db, user_id)
    totp_cred = next((c for c in creds if c.mfa_type == "totp"), None)
    if totp_cred is None or not totp_cred.totp_secret:
        raise FacultyMfaInvalidError(detail="No pending MFA enrollment found.")

    plain_secret = decrypt_totp_secret(totp_cred.totp_secret)
    counter = matching_totp_counter(plain_secret, code, drift_steps=1)
    if counter is None:
        raise FacultyMfaInvalidError(detail="Invalid verification code.")

    totp_cred.is_enrolled = True
    totp_cred.sign_count = counter
    totp_cred.last_used_at = datetime.now(UTC)

    codes = generate_recovery_codes(8)
    hashed = [hash_recovery_code(c) for c in codes]
    await repository.create_mfa_recovery_codes(db, user_id=user_id, hashed_codes=hashed)

    return codes


async def get_faculty_sections(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> list[SectionItem]:
    """Retrieve sections assigned to the faculty member (AC-002.6).

    Guarded: Returns 403 AUTH_MFA_ENROLLMENT_REQUIRED if MFA is not enrolled.
    """
    creds = await repository.get_user_mfa_credentials(db, user_id)
    if not any(c.is_enrolled for c in creds if c.mfa_type == "totp"):
        raise MfaEnrollmentRequiredError

    sections = await repository.get_faculty_sections(db, user_id)
    return [
        SectionItem(
            id=s.id,
            section_name=s.section_name,
            capacity=s.capacity,
            status=s.status,
        )
        for s in sections
    ]


async def get_section_roster(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    section_id: str | uuid.UUID,
) -> dict[str, Any]:
    """Retrieve section roster with ownership protection (AC-002.7).

    Guarded: If section is not owned by calling faculty, answers 404
    RESOURCE_NOT_FOUND (not 403) and logs authorization failure.
    """
    creds = await repository.get_user_mfa_credentials(db, user_id)
    if not any(c.is_enrolled for c in creds if c.mfa_type == "totp"):
        raise MfaEnrollmentRequiredError

    section = await repository.get_course_section(db, section_id)
    if section is None or section.faculty_id != user_id:
        if section is not None and section.faculty_id != user_id:
            logger.warning(
                "Authorization failure: Faculty %s attempted to access section %s "
                "assigned to faculty %s (BR-AUTH-005)",
                user_id,
                section_id,
                section.faculty_id,
            )
        raise ResourceNotFoundError

    return {
        "sectionId": str(section.id),
        "sectionName": section.section_name,
        "capacity": section.capacity,
        "status": section.status,
        "roster": [],
    }


async def list_auth_audit_logs(
    db: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[Any]:
    """Read the authentication audit ledger (AC-002.8)."""
    return list(await repository.list_auth_audit_logs(db, limit=limit, offset=offset))


def delete_auth_audit_logs() -> None:
    """Refuse audit record deletion (AC-002.8 / BR-AUTH-006)."""
    raise AuditImmutableError
