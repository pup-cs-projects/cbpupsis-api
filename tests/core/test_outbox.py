"""Tests for the transactional outbox.

The outbox exists to make one claim: a side effect staged alongside a business
change is delivered even if the process dies immediately afterwards. Everything
here is in service of proving that claim, or of proving the honest limits around
it.

The two assertions that matter most are the ones a passing suite would otherwise
never make:

- **Atomicity** — a rolled-back transaction leaves no message. This is why
  staging happens on the caller's session rather than a fresh one, and it is the
  single test that would catch someone "simplifying" that away.
- **Failure does not mark dispatched** — a handler that raises must leave the row
  retryable. The inverse bug is silent: rows marked delivered that never were.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.events import Event
from app.core.outbox import (
    DeliveryReceipt,
    OutboxMessage,
    OutboxStatus,
    add_receipt,
    already_delivered,
    claim_pending,
    mark_dispatched,
    mark_failed,
    pending_stats,
    publish_transactional,
    start_attempt,
)


async def _stage(
    db: AsyncSession, name: str = "test.event", **payload
) -> OutboxMessage:
    message = publish_transactional(db, Event(name=name, payload=payload))
    await db.commit()
    return message


class TestStaging:
    async def test_a_staged_message_becomes_a_row(self, db: AsyncSession) -> None:
        message = await _stage(db, "item.created", item_id="abc")

        assert message.id is not None
        assert message.status == OutboxStatus.PENDING
        assert message.attempts == 0
        assert message.payload == {"item_id": "abc"}

    async def test_a_rollback_leaves_no_message(self, db: AsyncSession) -> None:
        """The atomicity guarantee, stated as a test.

        This is the whole reason ``publish_transactional`` stages on the
        caller's session and does not commit: the event and the business change
        it announces share one transaction, so an event can never describe
        something that was rolled back. A version that opened its own session
        would look identical at the call site and pass every other test here.
        """
        publish_transactional(db, Event(name="item.created", payload={"x": 1}))
        await db.rollback()

        rows = (await db.execute(select(OutboxMessage))).scalars().all()
        assert rows == []

    async def test_publish_transactional_does_not_commit(
        self, db: AsyncSession
    ) -> None:
        """If it committed, the caller could not roll it back — which is the
        same bug as the test above, seen from the other side."""
        publish_transactional(db, Event(name="item.created"))

        assert db.in_transaction()
        await db.rollback()

    async def test_the_payload_survives_a_round_trip(self, db: AsyncSession) -> None:
        """JSONB on Postgres, JSON on SQLite — the variant has to work on both."""
        message = await _stage(db, "x", nested={"a": [1, 2]}, flag=True, none=None)
        db.expunge(message)

        fetched = await db.get(OutboxMessage, message.id)
        assert fetched is not None
        assert fetched.payload == {"nested": {"a": [1, 2]}, "flag": True, "none": None}


class TestClaiming:
    async def test_claims_a_pending_message(self, db: AsyncSession) -> None:
        message = await _stage(db)

        claimed = await claim_pending(db)

        assert [c.id for c in claimed] == [message.id]

    async def test_does_not_claim_a_dispatched_message(self, db: AsyncSession) -> None:
        message = await _stage(db)
        mark_dispatched(message)
        await db.commit()

        assert await claim_pending(db) == []

    async def test_does_not_claim_a_message_that_is_not_due_yet(
        self, db: AsyncSession
    ) -> None:
        """Backoff is implemented by pushing ``available_at`` forward, so a
        retry is simply a row that is not yet due."""
        message = await _stage(db)
        message.available_at = datetime.now(UTC) + timedelta(minutes=5)
        await db.commit()

        assert await claim_pending(db) == []

    async def test_respects_the_batch_size(self, db: AsyncSession) -> None:
        for _ in range(5):
            await _stage(db)

        assert len(await claim_pending(db, batch_size=2)) == 2

    async def test_claims_oldest_first(self, db: AsyncSession) -> None:
        old = await _stage(db, "first")
        new = await _stage(db, "second")
        old.available_at = datetime.now(UTC) - timedelta(hours=1)
        await db.commit()

        claimed = await claim_pending(db)
        assert [c.id for c in claimed] == [old.id, new.id]


class TestAttemptLifecycle:
    async def test_starting_an_attempt_counts_and_defers(
        self, db: AsyncSession
    ) -> None:
        """Counted and deferred in the claim transaction, before dispatch — so a
        crash mid-send leaves the row already backed off rather than retrying
        immediately and forever."""
        message = await _stage(db)
        before = message.available_at

        start_attempt(message)
        await db.commit()

        assert message.attempts == 1
        assert message.available_at > before

    async def test_backoff_grows_with_each_attempt(self, db: AsyncSession) -> None:
        message = await _stage(db)
        now = datetime.now(UTC)

        start_attempt(message, now=now)
        first = message.available_at - now
        start_attempt(message, now=now)
        second = message.available_at - now

        assert second > first

    async def test_backoff_is_capped(self, db: AsyncSession) -> None:
        """Without a ceiling the last retry is days out, long after anyone still
        wants the message delivered."""
        message = await _stage(db)
        now = datetime.now(UTC)
        message.attempts = 40

        start_attempt(message, now=now)

        cap = timedelta(seconds=settings.outbox_backoff_cap_seconds)
        assert message.available_at - now <= cap

    async def test_dispatched_is_terminal(self, db: AsyncSession) -> None:
        message = await _stage(db)

        mark_dispatched(message)
        await db.commit()

        assert message.status == OutboxStatus.DISPATCHED
        assert message.dispatched_at is not None

    async def test_a_failure_stays_pending_below_the_limit(
        self, db: AsyncSession
    ) -> None:
        """The assertion that pins the design: a failed delivery must remain
        retryable. Marking it dispatched would lose the message silently, which
        is exactly what routing failures through the event bus would do."""
        message = await _stage(db)
        start_attempt(message)

        mark_failed(message, RuntimeError("SES is down"))
        await db.commit()

        assert message.status == OutboxStatus.PENDING
        assert message.attempts == 1
        assert "SES is down" in (message.last_error or "")

    async def test_dead_letters_once_attempts_are_exhausted(
        self, db: AsyncSession
    ) -> None:
        message = await _stage(db)
        message.attempts = settings.outbox_max_attempts

        mark_failed(message, "gave up")
        await db.commit()

        assert message.status == OutboxStatus.FAILED

    async def test_a_dead_lettered_message_is_not_reclaimed(
        self, db: AsyncSession
    ) -> None:
        """Dead-letter means stop. A failed row stays for an operator to look
        at rather than being retried forever."""
        message = await _stage(db)
        message.attempts = settings.outbox_max_attempts
        mark_failed(message, "gave up")
        await db.commit()

        assert await claim_pending(db) == []

    async def test_the_error_text_is_truncated(self, db: AsyncSession) -> None:
        """A pathological traceback must not bloat the table storing it."""
        message = await _stage(db)

        mark_failed(message, "x" * 10_000)
        await db.commit()

        assert len(message.last_error or "") < 10_000


class TestDeliveryReceipts:
    async def test_a_receipt_records_a_delivery(self, db: AsyncSession) -> None:
        message = await _stage(db)

        assert not await already_delivered(db, message.id, "email")
        add_receipt(db, message.id, "email")
        await db.commit()

        assert await already_delivered(db, message.id, "email")

    async def test_receipts_are_per_channel(self, db: AsyncSession) -> None:
        """A message delivered by email may still be pending in-app."""
        message = await _stage(db)
        add_receipt(db, message.id, "email")
        await db.commit()

        assert await already_delivered(db, message.id, "email")
        assert not await already_delivered(db, message.id, "in_app")

    async def test_a_duplicate_receipt_is_refused_by_the_database(
        self, db: AsyncSession
    ) -> None:
        """The UNIQUE constraint IS the dedupe mechanism.

        Two workers can both read "no receipt" before either writes one, so an
        application-level check cannot close the race. The constraint can, which
        is why the email channel writes the receipt before calling SES and
        treats the violation as "already sent".
        """
        message = await _stage(db)
        add_receipt(db, message.id, "email")
        await db.commit()

        add_receipt(db, message.id, "email")
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()

    async def test_receipts_for_different_messages_coexist(
        self, db: AsyncSession
    ) -> None:
        first = await _stage(db)
        second = await _stage(db)
        add_receipt(db, first.id, "email")
        add_receipt(db, second.id, "email")
        await db.commit()

        rows = (await db.execute(select(DeliveryReceipt))).scalars().all()
        assert len(rows) == 2


class TestPendingStats:
    async def test_reports_an_empty_backlog(self, db: AsyncSession) -> None:
        stats = await pending_stats(db)

        assert stats == {"pending": 0, "oldest_pending_seconds": 0.0}

    async def test_counts_only_pending_messages(self, db: AsyncSession) -> None:
        await _stage(db)
        done = await _stage(db)
        mark_dispatched(done)
        await db.commit()

        assert (await pending_stats(db))["pending"] == 1

    async def test_reports_the_age_of_the_oldest_message(
        self, db: AsyncSession
    ) -> None:
        """``oldest_pending_seconds`` is the number worth alerting on: a worker
        wedged on a poison message keeps running and keeps logging, and only the
        age of the backlog reveals it."""
        message = await _stage(db)
        message.created_at = datetime.now(UTC) - timedelta(minutes=10)
        await db.commit()

        assert (await pending_stats(db))["oldest_pending_seconds"] > 500


class TestUnknownMessage:
    async def test_a_receipt_for_an_unknown_message_is_allowed(
        self, db: AsyncSession
    ) -> None:
        """``outbox_message_id`` carries no foreign key on purpose: receipts are
        pruned on a different schedule than the messages they guard, and a
        cascade would delete the very row that prevents a duplicate send."""
        add_receipt(db, uuid.uuid4(), "email")
        await db.commit()
