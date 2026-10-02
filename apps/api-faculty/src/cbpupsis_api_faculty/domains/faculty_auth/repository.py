"""Data access for the faculty authentication domain."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.audit import AuthAuditLog
from cbpupsis_database.models.auth import (
    IdempotencyKey,
    UserActiveSession,
    UserMfaCredential,
    UserMfaRecoveryCode,
)
from cbpupsis_database.models.sections import CourseSection
from cbpupsis_database.models.users import FacultyProfile, User, UserProfile


async def get_recent_failed_attempts(
    db: AsyncSession,
    attempted_id: str,
    since: datetime,
    user_id: uuid.UUID | None = None,
) -> Sequence[AuthAuditLog]:
    """Return failed login attempts for an attempted id or user within the window.

    SQL::
        SELECT id, user_id, attempted_id, success, failure_reason, created_at
        FROM auth_audit_logs
        WHERE (attempted_id = :attempted_id OR user_id = :user_id)
          AND success = false
          AND created_at >= :since
        ORDER BY created_at DESC
    """
    conditions = [AuthAuditLog.attempted_id == attempted_id]
    if user_id is not None:
        conditions.append(AuthAuditLog.user_id == user_id)

    stmt = (
        select(AuthAuditLog)
        .where(
            or_(*conditions),
            AuthAuditLog.success.is_(False),
            AuthAuditLog.created_at >= since,
        )
        .order_by(desc(AuthAuditLog.created_at))
    )
    result = await db.scalars(stmt)
    return result.all()


async def record_audit_log(
    db: AsyncSession,
    *,
    attempted_id: str,
    user_id: uuid.UUID | None,
    ip_address: str | None,
    user_agent: str | None,
    success: bool,
    failure_reason: str | None,
    mfa_used: bool = False,
    occurred_at: datetime | None = None,
) -> AuthAuditLog:
    """Record an append-only authentication audit entry.

    SQL::
        INSERT INTO auth_audit_logs (
            id, user_id, attempted_id, ip_address, user_agent,
            success, failure_reason, is_suspicious, mfa_used, created_at
        ) VALUES (
            :id, :user_id, :attempted_id, :ip_address, :user_agent,
            :success, :failure_reason, false, :mfa_used, :created_at
        )
    """
    entry = AuthAuditLog(
        id=uuid.uuid4(),
        user_id=user_id,
        attempted_id=attempted_id,
        ip_address=ip_address,
        user_agent=user_agent,
        success=success,
        failure_reason=failure_reason,
        is_suspicious=False,
        mfa_used=mfa_used,
        created_at=occurred_at or datetime.now(UTC),
    )
    db.add(entry)
    await db.flush()
    return entry


async def get_faculty_by_employee_id(
    db: AsyncSession,
    employee_id: str,
) -> tuple[User, FacultyProfile, UserProfile | None] | None:
    """Find a faculty member and associated profiles by employee id.

    SQL::
        SELECT users.*, faculty_profiles.*, user_profiles.*
        FROM faculty_profiles
        JOIN users ON faculty_profiles.user_id = users.id
        LEFT JOIN user_profiles ON user_profiles.user_id = users.id
        WHERE faculty_profiles.employee_id = :employee_id
    """
    stmt = (
        select(User, FacultyProfile, UserProfile)
        .join(FacultyProfile, FacultyProfile.user_id == User.id)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .where(FacultyProfile.employee_id == employee_id)
    )
    result = await db.execute(stmt)
    row = result.first()
    if row is None:
        return None
    user, faculty_profile, user_profile = row
    return user, faculty_profile, user_profile


async def get_faculty_by_user_id(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> tuple[User, FacultyProfile, UserProfile | None] | None:
    """Find a faculty member and associated profiles by user ID.

    SQL::
        SELECT users.*, faculty_profiles.*, user_profiles.*
        FROM users
        JOIN faculty_profiles ON faculty_profiles.user_id = users.id
        LEFT JOIN user_profiles ON user_profiles.user_id = users.id
        WHERE users.id = :user_id
    """
    stmt = (
        select(User, FacultyProfile, UserProfile)
        .join(FacultyProfile, FacultyProfile.user_id == User.id)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .where(User.id == user_id)
    )
    result = await db.execute(stmt)
    row = result.first()
    if row is None:
        return None
    user, faculty_profile, user_profile = row
    return user, faculty_profile, user_profile


async def get_user_mfa_credentials(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> Sequence[UserMfaCredential]:
    """Return all MFA credentials associated with a user.

    SQL::
        SELECT id, user_id, mfa_type, totp_secret, credential_id,
               public_key, sign_count, is_enrolled, last_used_at, created_at
        FROM user_mfa_credentials
        WHERE user_id = :user_id
    """
    stmt = select(UserMfaCredential).where(UserMfaCredential.user_id == user_id)
    result = await db.scalars(stmt)
    return result.all()


async def create_or_update_mfa_credential(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    totp_secret: str,
    is_enrolled: bool,
    last_used_counter: int = 0,
) -> UserMfaCredential:
    """Upsert a user TOTP MFA credential.

    SQL::
        INSERT INTO user_mfa_credentials (
            id, user_id, mfa_type, totp_secret, is_enrolled, sign_count, created_at
        ) VALUES (
            :id, :user_id, 'totp', :totp_secret, :is_enrolled, :sign_count, :created_at
        )
    """
    stmt = select(UserMfaCredential).where(
        UserMfaCredential.user_id == user_id,
        UserMfaCredential.mfa_type == "totp",
    )
    cred = (await db.scalars(stmt)).first()
    if cred is None:
        cred = UserMfaCredential(
            id=uuid.uuid4(),
            user_id=user_id,
            mfa_type="totp",
            totp_secret=totp_secret,
            is_enrolled=is_enrolled,
            sign_count=last_used_counter,
            created_at=datetime.now(UTC),
        )
        db.add(cred)
    else:
        cred.totp_secret = totp_secret
        cred.is_enrolled = is_enrolled
        if last_used_counter > 0:
            cred.sign_count = last_used_counter
            cred.last_used_at = datetime.now(UTC)
    await db.flush()
    return cred


async def is_challenge_spent(db: AsyncSession, jti: str) -> bool:
    """Return whether a challenge token JTI has already been consumed.

    SQL::
        SELECT id FROM idempotency_keys
        WHERE idempotency_key = :key
    """
    key = f"mfa_challenge:{jti}"
    stmt = select(IdempotencyKey.id).where(IdempotencyKey.idempotency_key == key)
    res = await db.scalar(stmt)
    return res is not None


async def mark_challenge_spent(
    db: AsyncSession,
    *,
    jti: str,
    user_id: uuid.UUID,
    expires_at: datetime,
) -> None:
    """Record a consumed challenge token JTI in shared storage.

    SQL::
        INSERT INTO idempotency_keys (
            id, idempotency_key, user_id, request_method, request_path,
            status, locked_at, expires_at, created_at
        ) VALUES (
            :id, :key, :user_id, 'POST', '/auth/mfa/verify',
            'spent', :now, :expires_at, :now
        )
    """
    key = f"mfa_challenge:{jti}"
    now = datetime.now(UTC)
    record = IdempotencyKey(
        id=uuid.uuid4(),
        idempotency_key=key,
        user_id=user_id,
        request_method="POST",
        request_path="/auth/mfa/verify",
        status="spent",
        locked_at=now,
        expires_at=expires_at,
        created_at=now,
    )
    db.add(record)
    await db.flush()


async def is_totp_code_spent(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    code: str,
    counter: int,
) -> bool:
    """Check if a TOTP code / interval has already been spent.

    SQL::
        SELECT id FROM idempotency_keys
        WHERE idempotency_key = :key
    """
    key = f"mfa_code:{user_id}:{counter}"
    stmt = select(IdempotencyKey.id).where(IdempotencyKey.idempotency_key == key)
    res = await db.scalar(stmt)
    return res is not None


async def mark_totp_code_spent(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    code: str,
    counter: int,
    expires_at: datetime,
) -> None:
    """Mark a TOTP interval as spent in shared store.

    SQL::
        INSERT INTO idempotency_keys (
            id, idempotency_key, user_id, request_method, request_path,
            status, locked_at, expires_at, created_at
        ) VALUES (
            :id, :key, :user_id, 'POST', '/auth/mfa/verify',
            'spent', :now, :expires_at, :now
        )
    """
    key = f"mfa_code:{user_id}:{counter}"
    now = datetime.now(UTC)
    record = IdempotencyKey(
        id=uuid.uuid4(),
        idempotency_key=key,
        user_id=user_id,
        request_method="POST",
        request_path="/auth/mfa/verify",
        status="spent",
        locked_at=now,
        expires_at=expires_at,
        created_at=now,
    )
    db.add(record)
    await db.flush()


async def create_active_session(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    session_token_hash: str,
    device_info: str,
    ip_address: str,
) -> UserActiveSession:
    """Create an active session record for tracking and concurrency control.

    SQL::
        INSERT INTO user_active_sessions (
            id, user_id, session_token_hash, device_info, ip_address,
            is_current, last_activity_at, created_at
        ) VALUES (
            :id, :user_id, :session_token_hash, :device_info, :ip_address,
            true, :now, :now
        )
    """
    now = datetime.now(UTC)
    session = UserActiveSession(
        id=uuid.uuid4(),
        user_id=user_id,
        session_token_hash=session_token_hash,
        device_info=device_info,
        ip_address=ip_address,
        is_current=True,
        last_activity_at=now,
        created_at=now,
    )
    db.add(session)
    await db.flush()
    return session


async def create_mfa_recovery_codes(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    hashed_codes: list[str],
) -> list[UserMfaRecoveryCode]:
    """Store hashed backup recovery codes for MFA.

    SQL::
        INSERT INTO user_mfa_recovery_codes (
            id, user_id, code_hash, is_used, created_at
        ) VALUES (
            :id, :user_id, :code_hash, false, :now
        )
    """
    now = datetime.now(UTC)
    records: list[UserMfaRecoveryCode] = []
    for h in hashed_codes:
        rec = UserMfaRecoveryCode(
            id=uuid.uuid4(),
            user_id=user_id,
            code_hash=h,
            is_used=False,
            created_at=now,
        )
        db.add(rec)
        records.append(rec)
    await db.flush()
    return records


async def list_auth_audit_logs(
    db: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
) -> Sequence[AuthAuditLog]:
    """List authentication audit logs in reverse chronological order.

    SQL::
        SELECT id, user_id, attempted_id, ip_address, user_agent,
               success, failure_reason, is_suspicious, mfa_used, created_at
        FROM auth_audit_logs
        ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset
    """
    stmt = (
        select(AuthAuditLog)
        .order_by(desc(AuthAuditLog.created_at))
        .limit(limit)
        .offset(offset)
    )
    result = await db.scalars(stmt)
    return result.all()


async def get_course_section(
    db: AsyncSession,
    section_id: uuid.UUID | str,
) -> CourseSection | None:
    """Find a course section by its primary key.

    SQL::
        SELECT id, academic_term_id, course_id, faculty_id,
               section_name, capacity, status
        FROM course_sections
        WHERE id = :section_id
    """
    try:
        sec_uuid = uuid.UUID(str(section_id))
    except ValueError:
        return None

    stmt = select(CourseSection).where(CourseSection.id == sec_uuid)
    return (await db.scalars(stmt)).first()


async def get_faculty_sections(
    db: AsyncSession,
    faculty_id: uuid.UUID,
) -> Sequence[CourseSection]:
    """Return all course sections assigned to a faculty member.

    SQL::
        SELECT id, academic_term_id, course_id, faculty_id,
               section_name, capacity, status
        FROM course_sections
        WHERE faculty_id = :faculty_id
    """
    stmt = select(CourseSection).where(CourseSection.faculty_id == faculty_id)
    result = await db.scalars(stmt)
    return result.all()
