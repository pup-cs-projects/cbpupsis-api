"""Business logic for the notifications domain — its public interface.

Two rules live here that nothing else can enforce:

**A user only ever sees their own.** Reading a notification list is inherently
scoped, and a by-id read of somebody else's answers 404 rather than 403 — a 403
confirms the id exists and turns the endpoint into an oracle for other people's
activity. The router cannot express this, because the answer depends on the row.

**A security notice cannot be switched off.** ``NOTIFICATION_TYPES`` marks those
types ``optional=False``, and :func:`set_preference` refuses to disable them. A
password-changed alert is what surfaces an account takeover to its victim, so an
attacker holding a session must not be able to silence it.

No SQL lives here: every read and write goes through ``repository.py``. This
module owns the rules and the transaction boundary.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.pagination import Page
from cbpupsis_database.models.notifications import Notification
from cbpupsis_shared.domains.notifications import repository
from cbpupsis_shared.domains.notifications.channels import (
    CHANNELS,
    Recipient,
    RenderedNotification,
)
from cbpupsis_shared.domains.notifications.constants import (
    NOTIFICATION_TYPES,
    Channel,
)
from cbpupsis_shared.domains.notifications.exceptions import (
    MandatoryNotificationError,
    NotificationNotFoundError,
    UnknownNotificationTypeError,
)
from cbpupsis_shared.domains.notifications.schemas import NotificationPreferenceRead


async def list_for_user(
    db: AsyncSession, user_id: uuid.UUID, limit: int = 50, offset: int = 0
) -> Page[Notification]:
    """Return a page of one user's notifications, newest first.

    Scoped by construction: there is no parameter that would widen it to another
    user, which is the point. A caller wanting somebody else's would need a
    different function, and that function would need a permission.
    """
    rows, total = await repository.list_for_user(
        db, user_id, limit=limit, offset=offset
    )
    return Page(items=rows, total=total, limit=limit, offset=offset)


async def unread_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Return how many of a user's notifications are unread."""
    return await repository.count_unread(db, user_id)


async def get_for_user(
    db: AsyncSession, notification_id: uuid.UUID, user_id: uuid.UUID
) -> Notification:
    """Return one notification the user owns, or raise
    :class:`NotificationNotFoundError`.

    Somebody else's notification raises the **same** error as one that does not
    exist. That is deliberate: distinguishing them would let a caller probe
    which ids are real.
    """
    notification = await repository.get_notification(db, notification_id)
    if notification is None or notification.user_id != user_id:
        raise NotificationNotFoundError(notification_id)
    return notification


async def mark_read(
    db: AsyncSession, notification_id: uuid.UUID, user_id: uuid.UUID
) -> Notification:
    """Mark one of the user's notifications as read.

    Idempotent: marking an already-read notification keeps the original
    timestamp, because the meaningful fact is when they first saw it.
    """
    notification = await get_for_user(db, notification_id, user_id)
    repository.mark_read(notification, datetime.now(UTC))
    await db.commit()
    await db.refresh(notification)
    return notification


async def mark_all_read(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Mark every unread notification for a user as read. Returns the count."""
    updated = await repository.mark_all_read(db, user_id, datetime.now(UTC))
    await db.commit()
    return updated


async def resolve_channels(
    db: AsyncSession, user_id: uuid.UUID, notification_type: str
) -> tuple[str, ...]:
    """Return the channels this notification should actually go out on.

    Starts from the type's declared defaults and applies the user's stored
    preferences. **Absence of a preference row means the default**, so a type
    added after a user signed up applies to them immediately — no backfill, and
    no account silently excluded because it predates the feature.

    A non-optional type ignores preferences entirely. Belt and braces with
    :func:`set_preference`, which refuses to store the row in the first place:
    two independent guards, because a row written by a migration or a script
    would bypass the first one.
    """
    spec = NOTIFICATION_TYPES.get(notification_type)
    if spec is None:
        raise UnknownNotificationTypeError(notification_type)
    if not spec.optional:
        return tuple(spec.default_channels)

    preferences = {
        (p.notification_type, p.channel): p.enabled
        for p in await repository.list_preferences(db, user_id)
    }
    return tuple(
        channel
        for channel in spec.default_channels
        if preferences.get((notification_type, channel), True)
    )


async def get_preferences(
    db: AsyncSession, user_id: uuid.UUID
) -> list[NotificationPreferenceRead]:
    """Return the effective preference for every known type and channel.

    Every combination is returned, not just the stored overrides: a settings
    screen needs the state a user is actually in, and rendering only the
    overrides would show an empty page to someone who has changed nothing.

    ``editable`` tells the client which toggles to disable, so a user is not
    offered a switch that :func:`set_preference` will refuse.
    """
    stored = {
        (p.notification_type, p.channel): p.enabled
        for p in await repository.list_preferences(db, user_id)
    }
    return [
        NotificationPreferenceRead(
            notification_type=notification_type,
            channel=Channel(channel),
            enabled=stored.get((notification_type, channel), True),
            editable=spec.optional,
        )
        for notification_type, spec in NOTIFICATION_TYPES.items()
        for channel in spec.default_channels
    ]


async def set_preference(
    db: AsyncSession,
    user_id: uuid.UUID,
    notification_type: str,
    channel: str,
    enabled: bool,
) -> None:
    """Store a user's choice for one type and channel.

    Refuses to disable a non-optional type — see the module docstring. Enabling
    one is allowed and is a no-op, so a client that submits a whole form does
    not have to special-case them.
    """
    spec = NOTIFICATION_TYPES.get(notification_type)
    if spec is None:
        raise UnknownNotificationTypeError(notification_type)
    if not spec.optional and not enabled:
        raise MandatoryNotificationError(notification_type)

    existing = await repository.get_preference(db, user_id, notification_type, channel)
    if existing is None:
        repository.add_preference(
            db, user_id, notification_type, channel, enabled=enabled
        )
    else:
        repository.update_preference(existing, enabled=enabled)
    await db.commit()


async def deliver(
    db: AsyncSession,
    *,
    recipient: Recipient,
    message: RenderedNotification,
    source_message_id: uuid.UUID | None = None,
) -> tuple[str, ...]:
    """Send one notification on every channel the user has left enabled.

    Returns the channels actually used, which is what the worker logs.

    A failure on any channel propagates, so the outbox retries the message. That
    means a message failing on its second channel is redelivered and the first
    channel runs again — which is exactly why every channel is idempotent on
    ``source_message_id``.
    """
    channels = await resolve_channels(db, recipient.user_id, message.notification_type)
    for name in channels:
        await CHANNELS[name].deliver(
            db,
            recipient=recipient,
            message=message,
            source_message_id=source_message_id,
        )
    return channels
