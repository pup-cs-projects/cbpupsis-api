"""Data access for the notifications domain. CRUD only: no rules, no authorization.

Every query here is scoped by ``user_id`` where one applies, but that is a filter,
not a permission check — deciding *whose* id may be passed is the service's job.
A repository that made that decision would fix it for every caller, including
jobs that legitimately act on someone else's behalf.

Transactions belong to the service: nothing here commits.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.notifications.models import Notification, NotificationPreference


def _for_user(user_id: uuid.UUID) -> Select[tuple[Notification]]:
    """Base query scoped to one user.

    Centralised so no caller has to remember the filter — forgetting it on a
    list endpoint is how one user's notifications end up in another's bell.
    """
    return select(Notification).where(Notification.user_id == user_id)


async def list_for_user(
    db: AsyncSession, user_id: uuid.UUID, limit: int, offset: int
) -> tuple[list[Notification], int]:
    """Return one page of a user's notifications and the total count.

    Newest first with ``id`` as a unique tiebreaker: notifications written in
    one batch share a ``created_at`` to the microsecond, and without the second
    key adjacent pages could repeat or skip one.

    Two statements — the count comes from the same filtered query before limit
    and offset, so it describes the whole set rather than the slice.

    SQL::

        -- 1. total matching rows, before limit/offset
        SELECT count(*) AS count_1
        FROM (SELECT notifications.user_id AS user_id,
                     notifications.notification_type AS notification_type,
                     notifications.title AS title, notifications.body AS body,
                     notifications.payload AS payload,
                     notifications.read_at AS read_at,
                     notifications.source_message_id AS source_message_id,
                     notifications.id AS id,
                     notifications.created_at AS created_at,
                     notifications.updated_at AS updated_at
              FROM notifications
              WHERE notifications.user_id = :user_id_1::UUID) AS anon_1

        -- 2. the page itself
        SELECT notifications.user_id, notifications.notification_type,
               notifications.title, notifications.body, notifications.payload,
               notifications.read_at, notifications.source_message_id,
               notifications.id, notifications.created_at,
               notifications.updated_at
        FROM notifications
        WHERE notifications.user_id = :user_id_1::UUID
        ORDER BY notifications.created_at DESC, notifications.id DESC
        LIMIT :param_1 OFFSET :param_2
    """
    query = _for_user(user_id)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    result = await db.execute(
        query.order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), total or 0


async def count_unread(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Return how many of a user's notifications are unread.

    The bell badge. Polled by every client on an interval, so it is the hottest
    query in the domain — served by the partial index on ``(user_id, read_at)``.

    SQL::

        SELECT count(*) AS count_1
        FROM notifications
        WHERE notifications.user_id = :user_id_1::UUID
          AND notifications.read_at IS NULL
    """
    count = await db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
    )
    return count or 0


async def get_notification(
    db: AsyncSession, notification_id: uuid.UUID
) -> Notification | None:
    """Return one notification by id, or ``None``.

    Not scoped to a user: the service compares the owner itself, so that a
    caller who may not read it gets a 404 rather than a filtered-out row that
    is indistinguishable from one that never existed.

    SQL::

        SELECT notifications.user_id, notifications.notification_type,
               notifications.title, notifications.body, notifications.payload,
               notifications.read_at, notifications.source_message_id,
               notifications.id, notifications.created_at,
               notifications.updated_at
        FROM notifications
        WHERE notifications.id = :id_1::UUID
    """
    return await db.scalar(
        select(Notification).where(Notification.id == notification_id)
    )


async def get_by_source_message(
    db: AsyncSession, source_message_id: uuid.UUID
) -> Notification | None:
    """Return the notification produced by an outbox message, if any.

    The read half of the in-app dedupe. Advisory only — the UNIQUE constraint on
    ``source_message_id`` is what actually prevents the duplicate, because two
    workers can both read ``None`` before either inserts. This exists so the
    common case skips the work without provoking an IntegrityError.

    SQL::

        SELECT notifications.user_id, notifications.notification_type,
               notifications.title, notifications.body, notifications.payload,
               notifications.read_at, notifications.source_message_id,
               notifications.id, notifications.created_at,
               notifications.updated_at
        FROM notifications
        WHERE notifications.source_message_id = :source_message_id_1::UUID
    """
    return await db.scalar(
        select(Notification).where(Notification.source_message_id == source_message_id)
    )


def add_notification(
    db: AsyncSession,
    user_id: uuid.UUID,
    notification_type: str,
    title: str,
    body: str,
    payload: dict[str, Any] | None = None,
    source_message_id: uuid.UUID | None = None,
) -> Notification:
    """Stage a new notification on the session and return it.

    Synchronous because ``Session.add`` does not touch the database — the INSERT
    happens when the service commits.

    Emits no SQL here. On the service's commit::

        INSERT INTO notifications (user_id, notification_type, title, body,
                                   payload, read_at, source_message_id, id)
        VALUES (:user_id::UUID, :notification_type, :title, :body, :payload,
                :read_at, :source_message_id::UUID, :id::UUID)
        RETURNING notifications.created_at, notifications.updated_at
    """
    notification = Notification(
        user_id=user_id,
        notification_type=notification_type,
        title=title,
        body=body,
        payload=payload or {},
        source_message_id=source_message_id,
    )
    db.add(notification)
    return notification


def mark_read(notification: Notification, read_at: datetime) -> Notification:
    """Stamp a loaded notification as read, if it was not already.

    Idempotent: re-reading keeps the original timestamp, because the meaningful
    fact is when the user first saw it, not when they last re-opened the list.

    The timestamp is passed in rather than generated here so the service decides
    the instant, and a caller marking several rows can give them all the same one.

    Emits no SQL here. On the service's commit::

        UPDATE notifications SET read_at=:read_at, updated_at=now()
        WHERE notifications.id = :id_1::UUID
    """
    if notification.read_at is None:
        notification.read_at = read_at
    return notification


async def mark_all_read(db: AsyncSession, user_id: uuid.UUID, read_at: datetime) -> int:
    """Mark every unread notification for a user as read. Returns the count.

    A set-based UPDATE rather than load-then-modify: one round trip regardless of
    how many are unread, and no risk of loading thousands of rows into memory to
    change one column on each.

    SQL::

        UPDATE notifications SET read_at=:read_at, updated_at=now()
        WHERE notifications.user_id = :user_id_1::UUID
          AND notifications.read_at IS NULL
    """
    result = await db.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=read_at)
    )
    return result.rowcount or 0


async def list_preferences(
    db: AsyncSession, user_id: uuid.UUID
) -> list[NotificationPreference]:
    """Return every preference row a user has expressed.

    Rows exist only where the user has overridden a default, so an empty list is
    the normal state and means "everything as shipped" — not "notifications off".

    SQL::

        SELECT notification_preferences.user_id,
               notification_preferences.notification_type,
               notification_preferences.channel,
               notification_preferences.enabled,
               notification_preferences.id,
               notification_preferences.created_at,
               notification_preferences.updated_at
        FROM notification_preferences
        WHERE notification_preferences.user_id = :user_id_1::UUID
    """
    result = await db.execute(
        select(NotificationPreference).where(NotificationPreference.user_id == user_id)
    )
    return list(result.scalars().all())


async def get_preference(
    db: AsyncSession, user_id: uuid.UUID, notification_type: str, channel: str
) -> NotificationPreference | None:
    """Return one preference row, or ``None`` if the user has not set it.

    SQL::

        SELECT notification_preferences.user_id,
               notification_preferences.notification_type,
               notification_preferences.channel,
               notification_preferences.enabled,
               notification_preferences.id,
               notification_preferences.created_at,
               notification_preferences.updated_at
        FROM notification_preferences
        WHERE notification_preferences.user_id = :user_id_1::UUID
          AND notification_preferences.notification_type = :notification_type_1
          AND notification_preferences.channel = :channel_1
    """
    return await db.scalar(
        select(NotificationPreference).where(
            NotificationPreference.user_id == user_id,
            NotificationPreference.notification_type == notification_type,
            NotificationPreference.channel == channel,
        )
    )


def add_preference(
    db: AsyncSession,
    user_id: uuid.UUID,
    notification_type: str,
    channel: str,
    enabled: bool,
) -> NotificationPreference:
    """Stage a new preference row and return it.

    Emits no SQL here. On the service's commit::

        INSERT INTO notification_preferences (user_id, notification_type,
                                              channel, enabled, id)
        VALUES (:user_id::UUID, :notification_type, :channel, :enabled, :id::UUID)
        RETURNING notification_preferences.created_at,
                  notification_preferences.updated_at
    """
    preference = NotificationPreference(
        user_id=user_id,
        notification_type=notification_type,
        channel=channel,
        enabled=enabled,
    )
    db.add(preference)
    return preference


def update_preference(
    preference: NotificationPreference, enabled: bool
) -> NotificationPreference:
    """Set an existing preference's flag.

    Emits no SQL here. On the service's commit::

        UPDATE notification_preferences SET enabled=:enabled, updated_at=now()
        WHERE notification_preferences.id = :id_1::UUID
    """
    preference.enabled = enabled
    return preference


async def prune_read(db: AsyncSession, older_than: datetime, batch_size: int) -> int:
    """Delete read notifications older than ``older_than``. Returns the count.

    **Unread notifications are never pruned**, however old. An unread row is
    something the user has not seen yet; deleting it silently is worse than a
    stale bell, and a user returning after a long absence should find what they
    missed rather than an empty list.

    Bounded by ``batch_size`` and meant to be called in a loop — an unbounded
    DELETE takes a long-lived lock on precisely the table large enough to need
    pruning.

    SQL::

        DELETE FROM notifications
        WHERE notifications.id IN (
            SELECT notifications.id
            FROM notifications
            WHERE notifications.read_at IS NOT NULL
              AND notifications.read_at < :read_at_1
            LIMIT :param_1)
    """
    doomed = (
        select(Notification.id)
        .where(
            Notification.read_at.is_not(None),
            Notification.read_at < older_than,
        )
        .limit(batch_size)
    )
    result = await db.execute(delete(Notification).where(Notification.id.in_(doomed)))
    return result.rowcount or 0
