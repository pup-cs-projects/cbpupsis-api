"""Data access for the student authentication flow.

All SQL construction for student authentication, active sessions, and audit logs
lives here.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.student_auth import (
    AuthAuditLog,
    StudentProfile,
    User,
    UserActiveSession,
    UserProfile,
)


async def get_student_login_context(
    db: AsyncSession, student_number: str
) -> tuple[User, UserProfile | None] | None:
    """Return the user and profile data needed for student login.

    SQL::

        SELECT users.*, user_profiles.*
        FROM student_profiles
        JOIN users ON users.id = student_profiles.user_id
        LEFT JOIN user_profiles ON user_profiles.user_id = users.id
        WHERE student_profiles.student_number = :student_number
          AND users.deleted_at IS NULL
    """
    stmt = (
        select(User, UserProfile)
        .join(StudentProfile, StudentProfile.user_id == User.id)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .where(
            StudentProfile.student_number == student_number,
            User.deleted_at.is_(None),
        )
    )
    row = (await db.execute(stmt)).first()
    return None if row is None else (row[0], row[1])


async def create_active_session(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    session_token_hash: str,
    ip_address: str,
    device_info: str = "",
    now: datetime,
) -> UserActiveSession:
    """Create a new active session record for server-side idle tracking.

    SQL::

        INSERT INTO user_active_sessions (
            id, user_id, session_token_hash, device_info, ip_address,
            is_current, last_activity_at, created_at
        )
        VALUES (
            :id, :user_id, :session_token_hash, :device_info, :ip_address,
            true, :now, :now
        )
    """
    session = UserActiveSession(
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


async def get_active_session_by_token_hash(
    db: AsyncSession, session_token_hash: str
) -> UserActiveSession | None:
    """Retrieve an active session by token hash.

    SQL::

        SELECT user_active_sessions.*
        FROM user_active_sessions
        WHERE user_active_sessions.session_token_hash = :session_token_hash
    """
    stmt = select(UserActiveSession).where(
        UserActiveSession.session_token_hash == session_token_hash
    )
    return await db.scalar(stmt)


async def touch_session(
    db: AsyncSession, session_id: uuid.UUID, touched_at: datetime
) -> None:
    """Update last_activity_at timestamp on active session.

    SQL::

        UPDATE user_active_sessions
        SET last_activity_at = :touched_at
        WHERE user_active_sessions.id = :session_id
    """
    stmt = (
        update(UserActiveSession)
        .where(UserActiveSession.id == session_id)
        .values(last_activity_at=touched_at)
    )
    await db.execute(stmt)


async def delete_active_session(db: AsyncSession, session_id: uuid.UUID) -> None:
    """Invalidate and remove session from the server-side store.

    SQL::

        DELETE FROM user_active_sessions
        WHERE user_active_sessions.id = :session_id
    """
    stmt = delete(UserActiveSession).where(UserActiveSession.id == session_id)
    await db.execute(stmt)


async def record_audit_log(
    db: AsyncSession,
    *,
    attempted_id: str,
    user_id: uuid.UUID | None,
    ip_address: str | None,
    user_agent: str | None,
    success: bool,
    failure_reason: str | None,
    occurred_at: datetime,
) -> AuthAuditLog:
    """Write an append-only authentication audit row.

    SQL::

        INSERT INTO auth_audit_logs (
            id, user_id, attempted_id, ip_address, user_agent, success,
            failure_reason, is_suspicious, mfa_used, created_at
        )
        VALUES (
            :id, :user_id, :attempted_id, :ip_address, :user_agent, :success,
            :failure_reason, false, false, :occurred_at
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
        mfa_used=False,
        created_at=occurred_at,
    )
    db.add(entry)
    await db.flush()
    return entry


async def get_recent_failed_attempts(
    db: AsyncSession, attempted_id: str, since: datetime
) -> Sequence[AuthAuditLog]:
    """Fetch failed attempts for this identifier within a time window.

    SQL::

        SELECT auth_audit_logs.*
        FROM auth_audit_logs
        WHERE auth_audit_logs.attempted_id = :attempted_id
          AND auth_audit_logs.success = false
          AND auth_audit_logs.created_at >= :since
        ORDER BY auth_audit_logs.created_at DESC
    """
    stmt = (
        select(AuthAuditLog)
        .where(
            AuthAuditLog.attempted_id == attempted_id,
            AuthAuditLog.success.is_(False),
            AuthAuditLog.created_at >= since,
        )
        .order_by(desc(AuthAuditLog.created_at))
    )
    result = await db.scalars(stmt)
    return result.all()


async def get_audit_logs_for_identifier(
    db: AsyncSession, attempted_id: str
) -> Sequence[AuthAuditLog]:
    """Fetch all audit logs recorded for a given attempted identifier.

    SQL::

        SELECT auth_audit_logs.*
        FROM auth_audit_logs
        WHERE auth_audit_logs.attempted_id = :attempted_id
        ORDER BY auth_audit_logs.created_at ASC
    """
    stmt = (
        select(AuthAuditLog)
        .where(AuthAuditLog.attempted_id == attempted_id)
        .order_by(AuthAuditLog.created_at.asc())
    )
    result = await db.scalars(stmt)
    return result.all()
