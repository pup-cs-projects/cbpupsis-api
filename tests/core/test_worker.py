"""Tests for the outbox worker — the relay that makes staged messages arrive.

``tests/core/test_outbox.py`` proves the storage layer keeps its promises. This
file proves the loop on top of it does: that a claimed message is delivered, that
a failure stays retryable, and that shutdown does not drop work in flight.

The assertion that matters most is the one in ``TestDurability``: a message
staged and committed is delivered by a *later* process. Everything else here
would also pass for the in-process event bus; only that one distinguishes a
durable outbox from a fire-and-forget task that died with its process.

Handlers are registered per test and torn down, because ``_HANDLERS`` is a
module-level dict: a handler left behind would run in every later test and make
failures depend on collection order.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_core.events import Event
from cbpupsis_shared import worker
from cbpupsis_shared.outbox import OutboxStatus, publish_transactional


@pytest.fixture(autouse=True)
def _clean_handlers():
    """Isolate the module-level handler registry between tests."""
    worker._HANDLERS.clear()
    yield
    worker._HANDLERS.clear()


@pytest.fixture
def received() -> list[Event]:
    """Register a recording handler for ``test.event`` and expose what it saw."""
    seen: list[Event] = []

    async def handler(event: Event, *, message_id: uuid.UUID) -> None:
        seen.append(event)

    worker.register_handler("test.event", handler)
    return seen


async def _stage(db: AsyncSession, name: str = "test.event", **payload):
    message = publish_transactional(db, Event(name=name, payload=payload))
    await db.commit()
    return message


class TestHandlerRegistry:
    def test_registering_makes_a_handler_findable(self) -> None:
        async def handler(event, *, message_id):
            pass

        worker.register_handler("x.y", handler)
        assert worker.handlers_for("x.y") == (handler,)

    def test_registering_twice_does_not_double_the_handler(self) -> None:
        """Load-bearing: the worker and the API both register subscribers, so a
        non-idempotent registry would deliver every message twice."""

        async def handler(event, *, message_id):
            pass

        worker.register_handler("x.y", handler)
        worker.register_handler("x.y", handler)
        assert len(worker.handlers_for("x.y")) == 1

    def test_an_unregistered_event_has_no_handlers(self) -> None:
        assert worker.handlers_for("nobody.listens") == ()


class TestProcessBatch:
    async def test_delivers_a_pending_message(
        self, db: AsyncSession, received: list[Event]
    ) -> None:
        message = await _stage(db, item_id="abc")

        claimed = await worker.process_batch(db)

        assert claimed == 1
        assert [e.payload for e in received] == [{"item_id": "abc"}]
        assert message.status == OutboxStatus.DISPATCHED
        assert message.dispatched_at is not None

    async def test_an_empty_outbox_is_a_no_op(self, db: AsyncSession) -> None:
        assert await worker.process_batch(db) == 0

    async def test_a_delivered_message_is_not_delivered_again(
        self, db: AsyncSession, received: list[Event]
    ) -> None:
        await _stage(db)

        await worker.process_batch(db)
        await worker.process_batch(db)

        assert len(received) == 1

    async def test_a_failing_handler_leaves_the_message_retryable(
        self, db: AsyncSession
    ) -> None:
        """The core reliability assertion: a failed delivery must NOT be marked
        dispatched. The inverse bug is silent — rows recorded as delivered that
        never were — and is exactly what routing through the event bus, which
        swallows handler exceptions, would cause."""

        async def broken(event: Event, *, message_id: uuid.UUID) -> None:
            raise RuntimeError("SES is down")

        worker.register_handler("test.event", broken)
        message = await _stage(db)

        await worker.process_batch(db)

        assert message.status == OutboxStatus.PENDING
        assert message.attempts == 1
        assert "SES is down" in (message.last_error or "")

    async def test_a_failure_does_not_stop_the_rest_of_the_batch(
        self, db: AsyncSession
    ) -> None:
        """One poison message must not block everything queued behind it."""

        async def selective(event: Event, *, message_id: uuid.UUID) -> None:
            if event.payload.get("boom"):
                raise RuntimeError("nope")

        worker.register_handler("test.event", selective)
        bad = await _stage(db, boom=True)
        good = await _stage(db, boom=False)

        assert await worker.process_batch(db) == 2
        assert bad.status == OutboxStatus.PENDING
        assert good.status == OutboxStatus.DISPATCHED

    async def test_repeated_failure_eventually_dead_letters(
        self, db: AsyncSession
    ) -> None:
        async def broken(event: Event, *, message_id: uuid.UUID) -> None:
            raise RuntimeError("still down")

        worker.register_handler("test.event", broken)
        message = await _stage(db)

        for _ in range(settings.outbox_max_attempts):
            # Backoff would otherwise defer it; make it due again each round.
            message.available_at = message.created_at
            await db.commit()
            await worker.process_batch(db)

        assert message.status == OutboxStatus.FAILED
        assert message.attempts == settings.outbox_max_attempts

    async def test_a_message_with_no_handler_is_discarded_not_retried(
        self, db: AsyncSession
    ) -> None:
        """An event can legitimately be emitted before anything subscribes —
        ``item.created`` is in that state today. Retrying it forever would fill
        the backlog with work nobody wants done."""
        message = await _stage(db, "nobody.listens")

        await worker.process_batch(db)

        assert message.status == OutboxStatus.DISPATCHED

    async def test_the_handler_receives_the_message_id(self, db: AsyncSession) -> None:
        """The idempotency key. Without it a handler cannot recognise work it
        has already done, and at-least-once delivery becomes unsafe."""
        seen: list[uuid.UUID] = []

        async def handler(event: Event, *, message_id: uuid.UUID) -> None:
            seen.append(message_id)

        worker.register_handler("test.event", handler)
        message = await _stage(db)

        await worker.process_batch(db)

        assert seen == [message.id]

    async def test_every_handler_for_an_event_runs(self, db: AsyncSession) -> None:
        calls: list[str] = []

        async def first(event: Event, *, message_id: uuid.UUID) -> None:
            calls.append("first")

        async def second(event: Event, *, message_id: uuid.UUID) -> None:
            calls.append("second")

        worker.register_handler("test.event", first)
        worker.register_handler("test.event", second)
        await _stage(db)

        await worker.process_batch(db)

        assert calls == ["first", "second"]


class TestDurability:
    async def test_a_message_survives_a_process_that_never_ran_it(
        self, db: AsyncSession, received: list[Event]
    ) -> None:
        """The claim the whole design exists to support.

        Stage a message and commit, then do nothing — the stand-in for a process
        that crashed, was deployed over, or was scaled in before the handler
        ever ran. The in-process event bus loses the event here. The outbox does
        not: the row is still pending, and the next worker to run delivers it.
        """
        await _stage(db, order_id="123")
        assert received == []  # nothing has run it yet

        # A later process — the restart — picks it up.
        await worker.process_batch(db)

        assert [e.payload for e in received] == [{"order_id": "123"}]

    async def test_a_message_staged_in_a_rolled_back_transaction_is_never_sent(
        self, db: AsyncSession, received: list[Event]
    ) -> None:
        """The other half of atomicity: no notification for something that did
        not happen."""
        publish_transactional(db, Event(name="test.event", payload={"x": 1}))
        await db.rollback()

        await worker.process_batch(db)

        assert received == []


class TestHeartbeat:
    async def test_the_tick_logs_the_backlog(
        self, db: AsyncSession, monkeypatch, caplog
    ) -> None:
        """``oldest_pending_seconds`` is the field an operator alerts on, so it
        has to actually be emitted. A worker wedged on a poison message keeps
        running and keeps logging; only the age of the backlog reveals it.

        ``_tick`` opens its own session against the application engine, which in
        this suite points at a database that is not running — so the session
        factory is redirected at the test's session. What is verified here is
        the heartbeat contract, not the connection.
        """

        class _Once:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr("cbpupsis_shared.worker.AsyncSessionLocal", _Once)
        await _stage(db)

        with caplog.at_level(logging.INFO, logger="cbpupsis_shared.worker"):
            claimed = await worker._tick()

        assert claimed == 1
        record = next(r for r in caplog.records if r.message == "outbox.tick")
        assert record.claimed == 1
        assert hasattr(record, "pending")
        assert hasattr(record, "oldest_pending_seconds")


class TestShutdown:
    async def test_the_loop_exits_promptly_when_shutdown_is_set(
        self, monkeypatch
    ) -> None:
        """SIGTERM only sets a flag — the loop must notice it while idle.

        With a plain ``asyncio.sleep`` the flag would go unread until the poll
        interval elapsed, so every task would take the full interval to drain
        and ECS would eventually SIGKILL it. The poll interval here is set far
        longer than the timeout, so this passes only if the wait is genuinely
        interruptible.
        """
        monkeypatch.setattr(settings, "outbox_poll_interval_seconds", 30.0)

        # Stub the listener: it opens a real connection, and this test is about
        # how fast shutdown is noticed — not how fast Postgres answers. Left
        # real, a slow connect under full-suite load makes the timing assertion
        # measure the wrong thing and fail intermittently.
        class _NoListener:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def wait(self, timeout: float) -> bool:
                await asyncio.sleep(timeout)
                return False

        monkeypatch.setattr(worker, "WakeupListener", _NoListener)

        captured: list[asyncio.Event] = []
        real = worker._install_signal_handlers

        def _capture(shutdown: asyncio.Event) -> None:
            captured.append(shutdown)
            real(shutdown)

        monkeypatch.setattr(worker, "_install_signal_handlers", _capture)

        async def _no_work() -> int:
            return 0

        monkeypatch.setattr(worker, "_tick", _no_work)

        task = asyncio.create_task(worker.run())
        while not captured:
            await asyncio.sleep(0.01)

        captured[0].set()  # what the SIGTERM handler does
        await asyncio.wait_for(task, timeout=2.0)  # NOT the 30s interval

    async def test_installing_signal_handlers_does_not_raise(self) -> None:
        """Windows has no ``add_signal_handler``; the worker runs in Linux
        containers but must still import and start locally."""
        worker._install_signal_handlers(asyncio.Event())


class TestTheEntrypointDoesNotDuplicateTheModule:
    """`python -m cbpupsis_shared.worker` must not load this module twice.

    A regression test for a bug that reached a running container: executing the
    package with ``-m`` runs the file as ``__main__``, and anything that later
    does ``from cbpupsis_shared import worker`` imports it a SECOND time under its
    real name. Two module objects, two ``_HANDLERS`` dicts — so
    ``register_handler`` writes into one while the loop reads the other.

    The symptom was maximally misleading: every message was marked *dispatched*
    with "no handler for outbox message", the worker logged a healthy heartbeat,
    and nothing was ever delivered. It was identified from the log prefix saying
    ``[__main__]`` rather than ``[cbpupsis_shared.worker]``.

    The fix is the ``if __name__ == "__main__"`` block importing ``main`` from
    the real module path, so both names resolve to one object.
    """

    def test_the_entrypoint_delegates_to_the_imported_module(self) -> None:
        source = pathlib.Path(worker.__file__).read_text(encoding="utf-8")
        entrypoint = source.split('if __name__ == "__main__":')[1]

        assert "from cbpupsis_shared.worker import main" in entrypoint, (
            "the __main__ block must delegate to the imported module, or the "
            "module is loaded twice and handlers register into the wrong copy"
        )

    def test_the_handler_registry_is_module_state(self) -> None:
        """What makes the duplication harmful: the registry is module-level, so
        two module objects means two registries."""
        import cbpupsis_shared.worker as imported

        assert imported._HANDLERS is worker._HANDLERS
