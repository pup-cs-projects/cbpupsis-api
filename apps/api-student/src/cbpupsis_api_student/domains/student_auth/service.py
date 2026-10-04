"""Service layer for student authentication (US-AUTH-001).

Implements:
- Format and check-digit validation without hashing invalid input (AC-001.2).
- Identical failure response for wrong birthdate and password (AC-001.3).
- 5-failure rolling 15-minute lockout with security email (AC-001.4).
- Active lockout refusal with remaining time (AC-001.5).
- Append-only audit logging in UTC+8 with no credentials logged (AC-001.6).
- Server-side idle session management at 15 minutes (AC-001.7).
- Scoped student ownership answering 404 on horizontal access (AC-001.8).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import NoReturn

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.student_auth import repository as student_repo
from cbpupsis_api_student.domains.student_auth.exceptions import (
    StudentAccountLockedError,
    StudentAuthFailedError,
    StudentSessionExpiredError,
)
from cbpupsis_api_student.domains.student_auth.schemas import (
    StudentLoginCredentials,
    StudentLoginRequest,
    StudentLoginResponse,
    StudentLoginResponseData,
)
from cbpupsis_api_student.domains.student_auth.validation import validate_student_id
from cbpupsis_core.emails import send_email
from cbpupsis_shared.domains.auth import client as auth_client

logger = logging.getLogger(__name__)

UTC_PLUS_8 = timezone(timedelta(hours=8))
LOCKOUT_THRESHOLD = 5
LOCKOUT_WINDOW_MINUTES = 15
LOCKOUT_DURATION_SECONDS = 900
IDLE_SESSION_TIMEOUT_MINUTES = 15


def _get_utc8_now() -> datetime:
    return datetime.now(UTC_PLUS_8)


async def login_student(
    db: AsyncSession,
    data: StudentLoginRequest,
    *,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[StudentLoginResponse, str]:
    """Execute the student login flow.

    Returns a tuple of (response_dto, refresh_token_str).
    """
    now = _get_utc8_now()

    # Step 1: Format & check-digit validation (AC-001.2)
    # Must raise 422 immediately BEFORE any DB credential query or password hashing.
    validate_student_id(data.student_number)

    # Step 2: Check rolling 15-minute lockout (AC-001.4, AC-001.5)
    window_start = now - timedelta(minutes=LOCKOUT_WINDOW_MINUTES)
    recent_failures = await student_repo.get_recent_failed_attempts(
        db, attempted_id=data.student_number, since=window_start
    )

    if len(recent_failures) >= LOCKOUT_THRESHOLD:
        # Account is locked: determine remaining lockout time from the latest failure
        latest_failure = recent_failures[0]
        # In case naive datetime is returned from SQLite, ensure timezone
        fail_time = latest_failure.created_at
        if fail_time.tzinfo is None:
            fail_time = fail_time.replace(tzinfo=UTC_PLUS_8)

        lockout_expiry = fail_time + timedelta(seconds=LOCKOUT_DURATION_SECONDS)
        remaining = int((lockout_expiry - now).total_seconds())
        remaining_seconds = max(1, remaining)

        # Log refused attempt while locked (AC-001.6)
        await student_repo.record_audit_log(
            db,
            attempted_id=data.student_number,
            user_id=None,
            ip_address=client_ip,
            user_agent=user_agent,
            success=False,
            failure_reason="account_locked",
            occurred_at=now,
        )
        await db.commit()

        raise StudentAccountLockedError(retry_after_seconds=remaining_seconds)

    # Step 3: Fetch student credentials from the student-auth repository.
    context = await student_repo.get_student_login_context(db, data.student_number)
    credentials = None
    if context is not None:
        user, profile = context
        credentials = StudentLoginCredentials(
            user_id=user.id,
            email=user.email,
            password_hash=user.password_hash,
            is_active=user.is_active,
            anonymized_at=user.anonymized_at,
            birthdate=profile.birthdate if profile else None,
        )

    if credentials is None:
        # Unknown student number: record failure and raise generic 401
        await _handle_login_failure(
            db,
            student_number=data.student_number,
            credentials=None,
            client_ip=client_ip,
            user_agent=user_agent,
            reason="unknown_student_number",
            now=now,
            recent_failures_count=len(recent_failures),
        )

    if not credentials.is_active or credentials.anonymized_at is not None:
        await _handle_login_failure(
            db,
            student_number=data.student_number,
            credentials=credentials,
            client_ip=client_ip,
            user_agent=user_agent,
            reason="inactive_account",
            now=now,
            recent_failures_count=len(recent_failures),
        )

    # Step 4: Compare credentials
    birthdate_match = (
        credentials.birthdate is not None and credentials.birthdate == data.birthdate
    )
    password_match = auth_client.check_password(
        data.password, credentials.password_hash
    )

    if not birthdate_match or not password_match:
        reason = "wrong_birthdate" if not birthdate_match else "wrong_password"
        await _handle_login_failure(
            db,
            student_number=data.student_number,
            credentials=credentials,
            client_ip=client_ip,
            user_agent=user_agent,
            reason=reason,
            now=now,
            recent_failures_count=len(recent_failures),
        )

    # Step 5: Successful authentication
    await student_repo.record_audit_log(
        db,
        attempted_id=data.student_number,
        user_id=credentials.user_id,
        ip_address=client_ip,
        user_agent=user_agent,
        success=True,
        failure_reason=None,
        occurred_at=now,
    )

    # Shared auth owns JWT construction and the refresh-token ledger.
    token_pair = await auth_client.issue_token_pair(db, credentials.user_id)

    # Create server-side active session in user_active_sessions (AC-001.7)
    token_hash = hashlib.sha256(token_pair.access_token.encode("utf-8")).hexdigest()
    await student_repo.create_active_session(
        db,
        user_id=credentials.user_id,
        session_token_hash=token_hash,
        ip_address=client_ip or "127.0.0.1",
        device_info=user_agent or "",
        now=now,
    )
    await db.commit()

    response_data = StudentLoginResponseData(
        access_token=token_pair.access_token,
        expires_in=900,
        token_type="bearer",
        role="student",
        refresh_token=token_pair.refresh_token,
    )

    response = StudentLoginResponse(
        status="success",
        data=response_data,
        access_token=token_pair.access_token,
        token_type="bearer",
        role="student",
        refresh_token=token_pair.refresh_token,
    )
    return response, token_pair.refresh_token


async def _handle_login_failure(
    db: AsyncSession,
    *,
    student_number: str,
    credentials: StudentLoginCredentials | None,
    client_ip: str | None,
    user_agent: str | None,
    reason: str,
    now: datetime,
    recent_failures_count: int,
) -> NoReturn:
    """Record audit log, evaluate lockout triggering, and raise generic 401 or 423."""
    user_id = credentials.user_id if credentials else None
    await student_repo.record_audit_log(
        db,
        attempted_id=student_number,
        user_id=user_id,
        ip_address=client_ip,
        user_agent=user_agent,
        success=False,
        failure_reason=reason,
        occurred_at=now,
    )
    await db.commit()

    new_failure_count = recent_failures_count + 1
    if new_failure_count >= LOCKOUT_THRESHOLD:
        # Trigger lockout: send security notice email (AC-001.4)
        recipient = str(credentials.email) if credentials else None
        if recipient:
            await send_email(
                to=recipient,
                subject="Security Alert: Account Locked",
                body=(
                    "Your CBPUPSIS student account has been locked for 15 minutes "
                    "due to "
                    "5 consecutive failed login attempts."
                ),
            )
        raise StudentAccountLockedError(retry_after_seconds=LOCKOUT_DURATION_SECONDS)

    # AC-001.3: Byte-identical generic 401 response
    raise StudentAuthFailedError()


async def validate_active_student_session(
    db: AsyncSession,
    access_token: str,
) -> uuid.UUID:
    """Validate that the token is present in the server-side session store and not idle.

    Enforces 15-minute idle timeout (AC-001.7).
    """
    now = _get_utc8_now()
    token_hash = hashlib.sha256(access_token.encode("utf-8")).hexdigest()

    session_record = await student_repo.get_active_session_by_token_hash(db, token_hash)
    if session_record is None:
        raise StudentSessionExpiredError()

    # Check idle duration
    last_activity = session_record.last_activity_at
    if last_activity.tzinfo is None:
        last_activity = last_activity.replace(tzinfo=UTC_PLUS_8)

    idle_duration = now - last_activity
    if idle_duration > timedelta(minutes=IDLE_SESSION_TIMEOUT_MINUTES):
        # Session expired server-side: invalidate from session store
        await student_repo.delete_active_session(db, session_record.id)
        await db.commit()
        raise StudentSessionExpiredError()

    # Session is active: touch last_activity_at
    await student_repo.touch_session(db, session_record.id, now)
    await db.commit()
    return session_record.user_id


async def logout_student(
    db: AsyncSession,
    access_token: str | None,
    refresh_token: str | None = None,
) -> None:
    """Terminate the active session on logout."""
    if refresh_token:
        await auth_client.revoke_refresh_token(db, refresh_token)
    if access_token:
        token_hash = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
        session_record = await student_repo.get_active_session_by_token_hash(
            db, token_hash
        )
        if session_record:
            await student_repo.delete_active_session(db, session_record.id)
            await db.commit()
