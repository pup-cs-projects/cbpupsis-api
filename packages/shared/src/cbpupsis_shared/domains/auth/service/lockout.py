"""Sign-in lockout, shared by every role and every sign-in step."""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_core.events import Event
from cbpupsis_database.models.auth import AuthFailureStep
from cbpupsis_shared.domains.auth import repository
from cbpupsis_shared.domains.auth.exceptions import AccountLockedError
from cbpupsis_shared.domains.auth.service.sessions import as_utc
from cbpupsis_shared.outbox import publish_transactional


async def require_not_locked(db: AsyncSession, subject_id: uuid.UUID) -> None:
    """Refuse a sign-in step while the subject is locked (HTTP 423).

    Refused attempts are not recorded as failures, so hammering a locked
    account cannot extend its lock.
    """
    await repository.acquire_authentication_lock(db, subject_id)
    lockout = await repository.get_authentication_lockout(db, subject_id)
    if lockout is None:
        return
    remaining = math.ceil(
        (as_utc(lockout.locked_until) - datetime.now(UTC)).total_seconds()
    )
    if remaining > 0:
        raise AccountLockedError(remaining)
    await repository.delete_authentication_lockout(db, subject_id)


async def record_sign_in_failure(
    db: AsyncSession,
    subject_id: uuid.UUID,
    step: AuthFailureStep,
    *,
    notify: bool = True,
) -> None:
    """Count one failed step and commit it; lock on reaching the threshold.

    The failure that reaches ``mfa_lockout_threshold`` inside the window raises
    :class:`AccountLockedError` (423) itself, for every role. Otherwise the
    caller raises its own sign-in error. ``notify=False`` for a pseudonymous
    subject: there is no account to send the lock notice to.
    """
    now = datetime.now(UTC)
    prior = await repository.count_authentication_failures(
        db,
        subject_id=subject_id,
        since=now - timedelta(minutes=settings.mfa_lockout_window_minutes),
    )
    repository.add_authentication_failure(
        db, subject_id=subject_id, step=step, occurred_at=now
    )
    if prior + 1 < settings.mfa_lockout_threshold:
        await db.commit()
        return
    locked_until = now + timedelta(minutes=settings.mfa_lockout_minutes)
    lockout = await repository.get_authentication_lockout(db, subject_id)
    if lockout is None:
        repository.add_authentication_lockout(
            db, subject_id=subject_id, locked_until=locked_until
        )
    else:
        repository.update_authentication_lockout(lockout, locked_until=locked_until)
    if notify:
        publish_transactional(
            db,
            Event(
                name="auth.account_locked",
                payload={
                    "user_id": str(subject_id),
                    "locked_until": locked_until.isoformat(),
                },
            ),
        )
    await db.commit()
    raise AccountLockedError(settings.mfa_lockout_minutes * 60)


async def clear_sign_in_failures(db: AsyncSession, subject_id: uuid.UUID) -> None:
    """Stage removal of a subject's failures and lock after a completed sign-in."""
    await repository.acquire_authentication_lock(db, subject_id)
    await repository.delete_authentication_failures(db, subject_id)
    await repository.delete_authentication_lockout(db, subject_id)
