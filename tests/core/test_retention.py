"""Tests for the retention job.

Pruning is the unglamorous half of the outbox: without it the queue keeps every
message it ever delivered, the claim index carries millions of rows nobody will
look at, and the table the worker polls degrades slowly and silently.

Two properties are load-bearing and neither is obvious from reading the job:

- **What must NOT be deleted.** A pending message is still owed to someone; an
  unread notification is something the user has not seen. Age is not a reason to
  drop either, and a prune that took them would be data loss disguised as
  housekeeping.
- **Receipts outlive the messages they guard.** A delivery receipt is what stops
  a redelivered message being sent twice. Pruning one while its message could
  still be retried re-opens exactly the duplicate window it exists to close.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.events import Event
from app.core.outbox import (
    OutboxMessage,
    OutboxStatus,
    add_receipt,
    publish_transactional,
)
from app.core.outbox.store import prune_dispatched, prune_failed, prune_receipts
from app.domains.notifications import repository as notifications_repository
from app.domains.notifications.models import Notification
from scripts.prune_retention import prune

OLD = datetime(2020, 1, 1, tzinfo=UTC)
CUTOFF = datetime(2021, 1, 1, tzinfo=UTC)


async def _message(db: AsyncSession, *, status: str, when: datetime) -> OutboxMessage:
    message = publish_transactional(db, Event(name="test.event"))
    await db.commit()
    message.status = status
    message.created_at = when
    if status == OutboxStatus.DISPATCHED:
        message.dispatched_at = when
    await db.commit()
    return message


class TestPruningOutboxMessages:
    async def test_deletes_an_old_dispatched_message(self, db: AsyncSession) -> None:
        await _message(db, status=OutboxStatus.DISPATCHED, when=OLD)

        deleted = await prune_dispatched(db, older_than=CUTOFF)
        await db.commit()

        assert deleted == 1
        assert (await db.execute(select(OutboxMessage))).scalars().all() == []

    async def test_keeps_a_recent_dispatched_message(self, db: AsyncSession) -> None:
        await _message(db, status=OutboxStatus.DISPATCHED, when=datetime.now(UTC))

        assert await prune_dispatched(db, older_than=CUTOFF) == 0

    async def test_never_deletes_a_pending_message(self, db: AsyncSession) -> None:
        """However old. A pending message is still owed to someone — deleting it
        is losing the delivery the whole outbox exists to guarantee."""
        await _message(db, status=OutboxStatus.PENDING, when=OLD)

        assert await prune_dispatched(db, older_than=CUTOFF) == 0
        assert await prune_failed(db, older_than=CUTOFF) == 0
        assert len((await db.execute(select(OutboxMessage))).scalars().all()) == 1

    async def test_dispatched_pruning_leaves_failed_alone(
        self, db: AsyncSession
    ) -> None:
        """They have different windows: a dead-lettered row is evidence and is
        kept far longer than a delivered one."""
        await _message(db, status=OutboxStatus.FAILED, when=OLD)

        assert await prune_dispatched(db, older_than=CUTOFF) == 0

    async def test_deletes_an_old_failed_message(self, db: AsyncSession) -> None:
        await _message(db, status=OutboxStatus.FAILED, when=OLD)

        deleted = await prune_failed(db, older_than=CUTOFF)
        await db.commit()

        assert deleted == 1

    async def test_respects_the_batch_size(self, db: AsyncSession) -> None:
        """Bounded because an unbounded DELETE takes a long-lived lock on
        precisely the table large enough to need pruning."""
        for _ in range(5):
            await _message(db, status=OutboxStatus.DISPATCHED, when=OLD)

        assert await prune_dispatched(db, older_than=CUTOFF, batch_size=2) == 2


class TestPruningReceipts:
    async def test_deletes_an_old_receipt(self, db: AsyncSession) -> None:
        message = await _message(db, status=OutboxStatus.DISPATCHED, when=OLD)
        receipt = add_receipt(db, message.id, "email")
        await db.commit()
        receipt.created_at = OLD
        await db.commit()

        deleted = await prune_receipts(db, older_than=CUTOFF)
        await db.commit()

        assert deleted == 1

    async def test_keeps_a_recent_receipt(self, db: AsyncSession) -> None:
        message = await _message(db, status=OutboxStatus.DISPATCHED, when=OLD)
        add_receipt(db, message.id, "email")
        await db.commit()

        assert await prune_receipts(db, older_than=CUTOFF) == 0


class TestPruningNotifications:
    async def test_deletes_an_old_read_notification(self, db: AsyncSession) -> None:
        notification = notifications_repository.add_notification(
            db, uuid.uuid4(), "item.created", "T", "B"
        )
        await db.commit()
        notification.read_at = OLD
        await db.commit()

        deleted = await notifications_repository.prune_read(db, CUTOFF, 5000)
        await db.commit()

        assert deleted == 1

    async def test_never_deletes_an_unread_notification(self, db: AsyncSession) -> None:
        """However old. An unread row is something the user has not seen — a
        person returning after a long absence should find what they missed, not
        an empty list."""
        notifications_repository.add_notification(
            db, uuid.uuid4(), "item.created", "T", "B"
        )
        await db.commit()

        assert await notifications_repository.prune_read(db, CUTOFF, 5000) == 0
        assert len((await db.execute(select(Notification))).scalars().all()) == 1


class TestTheWholeJob:
    async def test_prunes_every_table_and_reports_counts(
        self, db: AsyncSession
    ) -> None:
        dispatched = await _message(db, status=OutboxStatus.DISPATCHED, when=OLD)
        await _message(db, status=OutboxStatus.FAILED, when=OLD)
        await _message(db, status=OutboxStatus.PENDING, when=OLD)
        receipt = add_receipt(db, dispatched.id, "email")
        read = notifications_repository.add_notification(
            db, uuid.uuid4(), "item.created", "T", "B"
        )
        notifications_repository.add_notification(
            db, uuid.uuid4(), "item.created", "Unread", "B"
        )
        await db.commit()
        receipt.created_at = OLD
        read.read_at = OLD
        await db.commit()

        # Far enough in the future that every window has elapsed.
        deleted = await prune(db, now=datetime(2030, 1, 1, tzinfo=UTC))

        assert deleted["outbox_dispatched"] == 1
        assert deleted["outbox_failed"] == 1
        assert deleted["notifications_read"] == 1
        assert deleted["delivery_receipts"] == 1

        # The pending message and the unread notification survive.
        assert len((await db.execute(select(OutboxMessage))).scalars().all()) == 1
        assert len((await db.execute(select(Notification))).scalars().all()) == 1

    async def test_a_run_with_nothing_to_do_is_a_no_op(self, db: AsyncSession) -> None:
        deleted = await prune(db)

        assert set(deleted.values()) == {0}


class TestRetentionWindowsAreCoherent:
    def test_receipts_outlive_the_messages_they_guard(self) -> None:
        """The one ordering rule that is a correctness property rather than a
        preference: a receipt pruned while its message can still be retried
        re-opens the duplicate-send window it exists to close."""
        assert (
            settings.delivery_receipt_retention_days
            > settings.outbox_dispatched_retention_days
        )
        assert (
            settings.delivery_receipt_retention_days
            > settings.outbox_failed_retention_days
        )

    def test_failed_messages_are_kept_longer_than_delivered_ones(self) -> None:
        """A dead-lettered row is the only evidence of what went wrong."""
        assert (
            settings.outbox_failed_retention_days
            > settings.outbox_dispatched_retention_days
        )
