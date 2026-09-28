"""Turn an outbox event into a delivered notification.

Runs in the **worker process**, after the emitting request has long returned, so
it opens its own session — the rule ``audit/service.py`` states and for the same
reason: a request-scoped session is closed by then, and reusing one is a
use-after-free that surfaces as an intermittent ``InterfaceError`` under load
rather than a clean failure.

Failures propagate. The worker turns an exception into a retry, so a handler
that swallowed its own errors would have its messages marked delivered when they
were not.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.events import Event
from cbpupsis_database.session import AsyncSessionLocal
from cbpupsis_shared.domains.notifications import service
from cbpupsis_shared.domains.notifications.channels import (
    Recipient,
    RenderedNotification,
)
from cbpupsis_shared.domains.notifications.templates import render
from cbpupsis_shared.domains.users import client as users_client


async def deliver_event(event: Event, *, message_id: uuid.UUID) -> None:
    """Deliver one outbox event as a notification. Opens its own session."""
    async with AsyncSessionLocal() as db:
        await deliver_event_on(db, event, message_id=message_id)


async def deliver_event_on(
    db: AsyncSession, event: Event, *, message_id: uuid.UUID | None = None
) -> tuple[str, ...]:
    """Deliver one event on ``db``, and report the channels used.

    Split from :func:`deliver_event` so it can be driven against a test session.
    The session-opening wrapper is untestable in-process: it reaches the
    application engine, which the SQLite suite never sees.

    A payload with no ``user_id`` is a programming error in the emitter, not a
    delivery failure — it would retry forever. It is skipped and reported as
    delivering nothing.
    """
    raw_user_id = event.payload.get("user_id") or event.payload.get("owner_id")
    if raw_user_id is None:
        return ()

    user_id = uuid.UUID(str(raw_user_id))
    recipient = await _recipient_for(db, user_id)
    message = render(event)

    return await service.deliver(
        db, recipient=recipient, message=message, source_message_id=message_id
    )


async def _recipient_for(db: AsyncSession, user_id: uuid.UUID) -> Recipient:
    """Assemble a recipient once, so no channel has to query.

    Goes through the users domain's ``client``, never its models — the seam that
    keeps this domain extractable. A user that no longer exists yields a
    recipient with no address: the in-app row is still written, and the email
    channel degrades rather than raising, because a deleted account's address is
    never coming back and retrying cannot help.
    """
    try:
        user = await users_client.get_user(db, user_id)
    except Exception:  # a missing user must not wedge the queue
        return Recipient(user_id=user_id)
    return Recipient(user_id=user_id, email=user.email)


__all__ = ["Recipient", "RenderedNotification", "deliver_event", "deliver_event_on"]
