"""SQL for OAuth attempts and lease-fenced reconciliation state."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.google_calendar import (
    GoogleCalendarAccount,
    GoogleCalendarOAuthState,
)


async def account(db: AsyncSession, student_id: uuid.UUID, *, lock=False):
    """SQL:: SELECT * FROM google_calendar_accounts WHERE student_id=:id FOR UPDATE;"""
    query = select(GoogleCalendarAccount).where(
        GoogleCalendarAccount.student_id == student_id
    )
    if lock:
        query = query.with_for_update()
    return await db.scalar(query.execution_options(populate_existing=True))


async def ensure_account(db: AsyncSession, student_id: uuid.UUID):
    """SQL:: INSERT INTO google_calendar_accounts (...) ON CONFLICT DO NOTHING;"""
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    await db.execute(
        insert(GoogleCalendarAccount)
        .values(
            student_id=student_id,
            generation=uuid.uuid4(),
            targets={},
            attempts=0,
        )
        .on_conflict_do_nothing(index_elements=["student_id"])
    )
    return await account(db, student_id, lock=True)


async def save_account(
    db: AsyncSession, student_id: uuid.UUID, values: dict, *, lease=None, now=None
):
    """SQL::

    UPDATE google_calendar_accounts SET ...
    WHERE student_id=:id AND lease_id=:lease AND lease_until>:now;
    """
    query = update(GoogleCalendarAccount).where(
        GoogleCalendarAccount.student_id == student_id
    )
    if lease is not None:
        query = query.where(
            GoogleCalendarAccount.lease_id == lease,
            GoogleCalendarAccount.lease_until > now,
        )
    result = await db.execute(
        query.values(**values).execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def replace_state(db: AsyncSession, student_id: uuid.UUID, **values):
    """SQL::

    DELETE FROM google_calendar_oauth_states WHERE student_id=:id;
    INSERT INTO google_calendar_oauth_states (...) VALUES (...);
    """
    await invalidate_state(db, student_id)
    db.add(GoogleCalendarOAuthState(student_id=student_id, **values))


async def invalidate_state(db: AsyncSession, student_id: uuid.UUID):
    """SQL:: DELETE FROM google_calendar_oauth_states WHERE student_id=:id;"""
    await db.execute(
        delete(GoogleCalendarOAuthState).where(
            GoogleCalendarOAuthState.student_id == student_id
        )
    )


async def consume_state(
    db: AsyncSession,
    student_id: uuid.UUID,
    session_id: uuid.UUID,
    state_hash: str,
    now: datetime,
):
    """SQL::

    DELETE FROM google_calendar_oauth_states
    WHERE ... AND expires_at>:now RETURNING payload_cipher;
    """
    return await db.scalar(
        delete(GoogleCalendarOAuthState)
        .where(
            GoogleCalendarOAuthState.student_id == student_id,
            GoogleCalendarOAuthState.session_id == session_id,
            GoogleCalendarOAuthState.state_hash == state_hash,
            GoogleCalendarOAuthState.expires_at > now,
        )
        .returning(GoogleCalendarOAuthState.payload_cipher)
        .execution_options(synchronize_session=False)
    )


async def claim_due(db: AsyncSession, now: datetime):
    """SQL::

    SELECT * FROM google_calendar_accounts WHERE next_sync_at<=:now
    FOR UPDATE SKIP LOCKED LIMIT 1;
    """
    row = await db.scalar(
        select(GoogleCalendarAccount)
        .where(
            GoogleCalendarAccount.next_sync_at <= now,
            or_(
                GoogleCalendarAccount.refresh_cipher.is_not(None),
                GoogleCalendarAccount.revoke_cipher.is_not(None),
            ),
            or_(
                GoogleCalendarAccount.lease_until.is_(None),
                GoogleCalendarAccount.lease_until <= now,
            ),
        )
        .order_by(GoogleCalendarAccount.next_sync_at, GoogleCalendarAccount.student_id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        return None
    row.lease_id = uuid.uuid4()
    row.lease_until = now + timedelta(seconds=120)
    await db.commit()
    return row.student_id, row.lease_id


async def prune_states(db: AsyncSession, now: datetime):
    """SQL:: DELETE FROM google_calendar_oauth_states WHERE expires_at<=:now;"""
    await db.execute(
        delete(GoogleCalendarOAuthState).where(
            GoogleCalendarOAuthState.expires_at <= now
        )
    )
