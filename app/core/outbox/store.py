"""Outbox queries: staging, claiming, marking, and delivery receipts.

The package docstring explains why the outbox exists and why delivery is
at-least-once. This module holds the statements, and the two ordering rules that
are easy to get backwards and fail silently:

- **Stage before the commit.** :func:`publish_transactional` deliberately does
  not commit; calling it *after* the caller's commit puts the row in a different
  transaction (or none), which is precisely the gap the outbox exists to close.
- **Claim, then commit, then dispatch.** Holding the row lock across a slow SES
  call keeps a transaction open for the whole round trip, and a crash mid-send
  rolls the attempt counter back — an infinite retry loop on a poison message.

``app/core/`` has no ``repository.py`` and ``test_architecture.py`` only scans
``app/domains/``, so the SQL-lives-in-the-repository rule does not reach here by
the letter of the guard. It is followed anyway: every public function below
documents the statement it runs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.events import Event
from app.core.outbox.constants import (
    LAST_ERROR_MAX_LENGTH,
    OutboxStatus,
)
from app.core.outbox.models import DeliveryReceipt, OutboxMessage


def publish_transactional(db: AsyncSession, event: Event) -> OutboxMessage:
    """Stage ``event`` for durable delivery on the caller's session.

    Synchronous, and it **does not commit** — both deliberate. ``Session.add``
    touches no database, so the INSERT is emitted by the caller's own
    ``await db.commit()``, which is what makes the event and the business change
    atomic::

        item = repository.add_item(db, ...)
        publish_transactional(db, Event(name="item.created", payload={...}))
        await db.commit()          # both rows, one transaction

    A version that opened its own session or committed here would look identical
    at the call site and quietly reintroduce the gap this module exists to close.

    Emits no SQL here. On the caller's commit::

        INSERT INTO outbox_messages (event_name, payload, status, available_at,
                                     attempts, last_error, dispatched_at,
                                     created_at, id)
        VALUES (:event_name, :payload, :status, :available_at, :attempts,
                :last_error, :dispatched_at, :created_at, :id::UUID)
    """
    message = OutboxMessage(
        event_name=event.name,
        payload=dict(event.payload),
        status=OutboxStatus.PENDING,
        available_at=datetime.now(UTC),
    )
    db.add(message)
    return message


async def claim_pending(
    db: AsyncSession, *, batch_size: int | None = None, now: datetime | None = None
) -> list[OutboxMessage]:
    """Claim a batch of due messages for this worker, and no other.

    ``FOR UPDATE SKIP LOCKED`` is what makes multiple workers safe with no
    coordination: a worker walks past rows another transaction holds instead of
    blocking on them, so N workers partition the batch by construction.

    On SQLite the lock clause is silently dropped by SQLAlchemy rather than
    raising, so the claim logic is testable in-process — but genuine contention
    between two workers is a Postgres behaviour and needs an integration test to
    prove. The suite documents that gap rather than implying otherwise.

    The caller must commit before dispatching; see :func:`start_attempt`.

    SQL::

        SELECT outbox_messages.event_name, outbox_messages.payload,
               outbox_messages.status, outbox_messages.available_at,
               outbox_messages.attempts, outbox_messages.last_error,
               outbox_messages.dispatched_at, outbox_messages.created_at,
               outbox_messages.id
        FROM outbox_messages
        WHERE outbox_messages.status = :status_1
          AND outbox_messages.available_at <= :available_at_1
        ORDER BY outbox_messages.available_at
        LIMIT :param_1 FOR UPDATE SKIP LOCKED
    """
    result = await db.execute(
        select(OutboxMessage)
        .where(
            OutboxMessage.status == OutboxStatus.PENDING,
            OutboxMessage.available_at <= (now or datetime.now(UTC)),
        )
        .order_by(OutboxMessage.available_at)
        .limit(batch_size or settings.outbox_batch_size)
        .with_for_update(skip_locked=True)
    )
    return list(result.scalars().all())


def start_attempt(message: OutboxMessage, *, now: datetime | None = None) -> None:
    """Record that delivery is being attempted, and push the next retry out.

    Called on a claimed row **before** the caller commits and dispatches. Doing
    it in the claim transaction is the whole point: if the process dies during
    the send, the row is already counted and already deferred, so it retries with
    backoff rather than immediately and forever.

    Emits no SQL here. On the caller's commit::

        UPDATE outbox_messages
        SET status=:status, available_at=:available_at, attempts=:attempts
        WHERE outbox_messages.id = :id_1::UUID
    """
    at = now or datetime.now(UTC)
    message.attempts += 1
    message.available_at = at + _backoff(message.attempts)


def mark_dispatched(message: OutboxMessage, *, now: datetime | None = None) -> None:
    """Mark a message delivered. Terminal, and the only success state.

    Emits no SQL here. On the caller's commit::

        UPDATE outbox_messages
        SET status=:status, dispatched_at=:dispatched_at
        WHERE outbox_messages.id = :id_1::UUID
    """
    message.status = OutboxStatus.DISPATCHED
    message.dispatched_at = now or datetime.now(UTC)


def mark_failed(message: OutboxMessage, error: BaseException | str) -> None:
    """Record a failed attempt, dead-lettering once the attempts are exhausted.

    Below the limit the row stays ``pending`` with the ``available_at`` that
    :func:`start_attempt` already pushed forward, so it is simply retried later.
    At the limit it becomes ``failed`` — the dead-letter state — and stays for an
    operator to inspect rather than being deleted or retried forever.

    Emits no SQL here. On the caller's commit::

        UPDATE outbox_messages
        SET status=:status, last_error=:last_error
        WHERE outbox_messages.id = :id_1::UUID
    """
    message.last_error = str(error)[:LAST_ERROR_MAX_LENGTH]
    if message.attempts >= settings.outbox_max_attempts:
        message.status = OutboxStatus.FAILED


async def already_delivered(
    db: AsyncSession, message_id: uuid.UUID, channel: str
) -> bool:
    """Return whether this message was already delivered on this channel.

    The read half of the email dedupe. It is advisory only — the UNIQUE
    constraint on ``delivery_receipts`` is what actually prevents the duplicate,
    because two workers can both read "no receipt" before either writes one.
    This exists so the common case skips the work without provoking an
    IntegrityError.

    SQL::

        SELECT delivery_receipts.outbox_message_id, delivery_receipts.channel,
               delivery_receipts.created_at, delivery_receipts.id
        FROM delivery_receipts
        WHERE delivery_receipts.outbox_message_id = :outbox_message_id_1::UUID
          AND delivery_receipts.channel = :channel_1
    """
    receipt = await db.scalar(
        select(DeliveryReceipt).where(
            DeliveryReceipt.outbox_message_id == message_id,
            DeliveryReceipt.channel == channel,
        )
    )
    return receipt is not None


def add_receipt(
    db: AsyncSession, message_id: uuid.UUID, channel: str
) -> DeliveryReceipt:
    """Stage proof that this message was delivered on this channel.

    Written **before** the outbound call, so the failure window is "receipt
    committed, send never happened" — which loses a message rather than
    duplicating one. The message stays ``pending``, so the next attempt retries
    it. Failing toward a retry is correct; failing toward a duplicate
    password-reset email is not.

    Emits no SQL here. On the caller's commit::

        INSERT INTO delivery_receipts (outbox_message_id, channel, created_at, id)
        VALUES (:outbox_message_id::UUID, :channel, :created_at, :id::UUID)
    """
    receipt = DeliveryReceipt(outbox_message_id=message_id, channel=channel)
    db.add(receipt)
    return receipt


async def pending_stats(db: AsyncSession) -> dict[str, Any]:
    """Return the numbers a worker heartbeat reports.

    ``oldest_pending_seconds`` is the one worth alerting on: a worker wedged on a
    poison message keeps running, keeps logging, and looks healthy by every other
    measure — only the age of the backlog reveals it.

    Two statements.

    SQL::

        -- 1. how many are waiting
        SELECT count(*) AS count_1
        FROM outbox_messages
        WHERE outbox_messages.status = :status_1

        -- 2. how long the oldest has waited
        SELECT min(outbox_messages.created_at) AS min_1
        FROM outbox_messages
        WHERE outbox_messages.status = :status_1
    """
    pending = await db.scalar(
        select(func.count())
        .select_from(OutboxMessage)
        .where(OutboxMessage.status == OutboxStatus.PENDING)
    )
    oldest = await db.scalar(
        select(func.min(OutboxMessage.created_at)).where(
            OutboxMessage.status == OutboxStatus.PENDING
        )
    )
    age = 0.0
    if oldest is not None:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - oldest).total_seconds()
    return {"pending": pending or 0, "oldest_pending_seconds": round(age, 1)}


def _backoff(attempts: int) -> timedelta:
    """Exponential backoff, capped.

    Private, so it needs no ``SQL::`` block — it runs none. Capped because
    doubling without a ceiling turns the eighth retry into a wait measured in
    days, long after anyone would still want the message delivered.
    """
    seconds = settings.outbox_backoff_base_seconds * (2 ** max(attempts - 1, 0))
    return timedelta(seconds=min(seconds, settings.outbox_backoff_cap_seconds))


async def prune_dispatched(
    db: AsyncSession, *, older_than: datetime, batch_size: int | None = None
) -> int:
    """Delete delivered messages older than ``older_than``. Returns the count.

    Bounded by ``batch_size`` and meant to be called in a loop: an unbounded
    DELETE takes a long-lived lock and can time out on exactly the table big
    enough to need pruning. Deleting in batches lets other work interleave and
    keeps each statement short.

    Only ``dispatched`` rows. A pending message is still owed to someone and a
    failed one is the only evidence of what went wrong, so neither is eligible
    however old it is.

    SQL::

        DELETE FROM outbox_messages
        WHERE outbox_messages.id IN (
            SELECT outbox_messages.id
            FROM outbox_messages
            WHERE outbox_messages.status = :status_1
              AND outbox_messages.dispatched_at < :dispatched_at_1
            LIMIT :param_1)
    """
    doomed = (
        select(OutboxMessage.id)
        .where(
            OutboxMessage.status == OutboxStatus.DISPATCHED,
            OutboxMessage.dispatched_at < older_than,
        )
        .limit(batch_size or settings.retention_batch_size)
    )
    result = await db.execute(delete(OutboxMessage).where(OutboxMessage.id.in_(doomed)))
    return result.rowcount or 0


async def prune_failed(
    db: AsyncSession, *, older_than: datetime, batch_size: int | None = None
) -> int:
    """Delete dead-lettered messages older than ``older_than``.

    Kept far longer than delivered ones, because a failed row is the only record
    of what broke — but not forever. A dead-letter queue nobody empties stops
    being a queue and becomes permanent storage that no alert ever fires on.

    Keyed on ``created_at`` rather than ``dispatched_at``, which is NULL for a
    row that never got delivered.

    SQL::

        DELETE FROM outbox_messages
        WHERE outbox_messages.id IN (
            SELECT outbox_messages.id
            FROM outbox_messages
            WHERE outbox_messages.status = :status_1
              AND outbox_messages.created_at < :created_at_1
            LIMIT :param_1)
    """
    doomed = (
        select(OutboxMessage.id)
        .where(
            OutboxMessage.status == OutboxStatus.FAILED,
            OutboxMessage.created_at < older_than,
        )
        .limit(batch_size or settings.retention_batch_size)
    )
    result = await db.execute(delete(OutboxMessage).where(OutboxMessage.id.in_(doomed)))
    return result.rowcount or 0


async def prune_receipts(
    db: AsyncSession, *, older_than: datetime, batch_size: int | None = None
) -> int:
    """Delete delivery receipts older than ``older_than``.

    **Must be pruned more slowly than the messages they guard.** A receipt is
    what stops a redelivered message being sent twice; deleting one while its
    message is still pending re-opens exactly the duplicate window the receipt
    exists to close. The default retention is deliberately longer than both
    message windows, and :func:`app.core.outbox.store.prune_receipts` should
    never be given a shorter one.

    SQL::

        DELETE FROM delivery_receipts
        WHERE delivery_receipts.id IN (
            SELECT delivery_receipts.id
            FROM delivery_receipts
            WHERE delivery_receipts.created_at < :created_at_1
            LIMIT :param_1)
    """
    doomed = (
        select(DeliveryReceipt.id)
        .where(DeliveryReceipt.created_at < older_than)
        .limit(batch_size or settings.retention_batch_size)
    )
    result = await db.execute(
        delete(DeliveryReceipt).where(DeliveryReceipt.id.in_(doomed))
    )
    return result.rowcount or 0
