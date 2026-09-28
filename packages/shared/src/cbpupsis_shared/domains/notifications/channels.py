"""Delivery channels: the seam that makes push or SMS a new class, not a rewrite.

A channel takes a rendered notification and gets it in front of a person. Email
and in-app ship here; push and SMS are deliberately absent, and adding one is a
class plus one entry in :data:`CHANNELS` — no change to the service, the worker,
or the outbox.

Mirrors the ``EmailSender`` Protocol in ``cbpupsis_core.emails``: a runtime-checkable
Protocol, keyword-only arguments, one verb. Two differences, both deliberate:

- **:meth:`Channel.deliver` raises on failure**, where ``send_email`` returns a
  bool. The outbox has to distinguish success from failure to drive its retry,
  and a bool that gets ignored is how a "must not be lost" system loses things.
  ``EmailChannel`` is where those two contracts meet, and it converts one to the
  other explicitly.
- **It takes a :class:`Recipient`, not an address.** In-app delivery needs a
  user id, email needs an address, a future SMS channel needs a phone number.
  Assembling that once, in the service, means a channel never queries.

Every channel must be **idempotent**, because delivery is at-least-once: a
message redelivered after a crash must not produce a second email or a second
bell entry. Both implementations here lean on a UNIQUE constraint rather than an
application-level check, since two workers can both observe "not yet delivered"
before either writes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.emails import send_email
from cbpupsis_shared.domains.notifications import repository
from cbpupsis_shared.domains.notifications.constants import Channel as ChannelName
from cbpupsis_shared.outbox import add_receipt, already_delivered


@dataclass(frozen=True)
class Recipient:
    """Who a notification is going to, with everything a channel might need.

    Assembled once by the service from the users domain, so no channel has to
    query — which is what keeps a channel a pure delivery mechanism and keeps
    this domain from reaching into another's tables.

    ``email`` is optional because a deleted account's address is scrubbed, and a
    channel must degrade rather than crash on one.
    """

    user_id: uuid.UUID
    email: str | None = None


@dataclass(frozen=True)
class RenderedNotification:
    """The message itself, already turned into words.

    Rendering happens before dispatch so every channel shows the same text, and
    so a template failure is caught once rather than separately per channel.
    """

    notification_type: str
    title: str
    body: str
    payload: dict[str, object]


@runtime_checkable
class Channel(Protocol):
    """One way of getting a notification to a person."""

    #: The channel's identity, matching a ``constants.Channel`` value. Stored
    #: on delivery receipts and preference rows, so it must be stable — renaming
    #: one orphans every preference that referenced it.
    name: str

    async def deliver(
        self,
        db: AsyncSession,
        *,
        recipient: Recipient,
        message: RenderedNotification,
        source_message_id: uuid.UUID | None = None,
    ) -> None:
        """Deliver ``message`` to ``recipient``, or raise.

        Raising is the contract: the caller turns an exception into a retry, so
        a channel that swallowed its own failures would have its messages marked
        delivered when they were not.

        Must be idempotent on ``source_message_id`` — see the module docstring.
        """
        ...


class InAppChannel:
    """Writes a row the bell icon reads.

    Idempotent through the UNIQUE constraint on
    ``notifications.source_message_id``: a redelivered message tries to insert a
    second row, the database refuses, and that refusal is treated as success —
    because it means the first delivery already happened.
    """

    name = ChannelName.IN_APP

    async def deliver(
        self,
        db: AsyncSession,
        *,
        recipient: Recipient,
        message: RenderedNotification,
        source_message_id: uuid.UUID | None = None,
    ) -> None:
        if source_message_id is not None:
            existing = await repository.get_by_source_message(db, source_message_id)
            if existing is not None:
                return

        repository.add_notification(
            db,
            user_id=recipient.user_id,
            notification_type=message.notification_type,
            title=message.title,
            body=message.body,
            payload=dict(message.payload),
            source_message_id=source_message_id,
        )
        try:
            await db.commit()
        except IntegrityError:
            # Lost the race: another worker inserted the same source_message_id
            # between the check above and this commit. The notification exists,
            # which is the outcome we wanted.
            await db.rollback()


class EmailChannel:
    """Sends the notification as an email.

    Two things make this safe under at-least-once delivery, and both matter:

    **The receipt is written before the send.** ``send_email`` has no
    idempotency — two calls produce two emails — so something has to remember
    that a send already happened, and it has to survive a crash. The failure
    window shrinks to "receipt committed, process died before SES was called",
    which loses a message rather than duplicating one; the outbox message is
    still pending, so the next attempt retries it. Failing toward a retry is
    correct; failing toward a duplicate password-reset email is not.

    **A false return becomes an exception.** ``send_email`` never raises and
    reports failure as ``False``, which is right for a best-effort caller in a
    request path. Here it is wrong: the outbox must know, or it marks the
    message delivered and the guarantee evaporates. This is the one place the
    two philosophies meet.
    """

    name = ChannelName.EMAIL

    async def deliver(
        self,
        db: AsyncSession,
        *,
        recipient: Recipient,
        message: RenderedNotification,
        source_message_id: uuid.UUID | None = None,
    ) -> None:
        if recipient.email is None:
            # A scrubbed or deleted account. Not an error worth retrying: the
            # address is never coming back.
            return

        if source_message_id is not None:
            if await already_delivered(db, source_message_id, self.name):
                return
            add_receipt(db, source_message_id, self.name)
            try:
                await db.commit()
            except IntegrityError:
                # Another worker claimed this send first.
                await db.rollback()
                return

        sent = await send_email(
            to=recipient.email, subject=message.title, body=message.body
        )
        if not sent:
            raise EmailDeliveryError(recipient.email)


class EmailDeliveryError(RuntimeError):
    """``send_email`` reported failure.

    A plain exception rather than a domain error: it never reaches an HTTP
    handler, because the only caller is the worker, which turns it into a retry.
    """

    def __init__(self, address: str) -> None:
        super().__init__(f"Email delivery failed for {address}")


#: Every channel the application can deliver on, by name.
#:
#: Adding push is: write the class, add it here. The service resolves channels
#: from preferences and looks them up in this dict, so nothing else changes.
CHANNELS: dict[str, Channel] = {
    ChannelName.IN_APP: InAppChannel(),
    ChannelName.EMAIL: EmailChannel(),
}
