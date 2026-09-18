"""Tests for the LISTEN/NOTIFY wake-up path.

This is an optimisation, not a delivery mechanism: the outbox row is already
durable before any of this runs, so every failure here must degrade into "one
poll interval slower" rather than "message lost". These tests pin that.

What is **not** covered in-process, honestly: the actual round trip. LISTEN needs
a session-mode Postgres connection, and this suite runs on SQLite — so the
notify, the listen, and the delivery are verified against the running stack
instead. ``tests/conftest.py`` already documents this trade for dialect-specific
behaviour.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.outbox import WakeupListener, notify_wakeup
from app.core.outbox.constants import WAKEUP_CHANNEL
from app.core.outbox.wakeup import listener_dsn


class TestListenerDsn:
    def test_prefers_the_direct_url(self, monkeypatch) -> None:
        """The pooled URL is PgBouncer in transaction mode and will not carry
        LISTEN. Getting this wrong is silent: notifications work but arrive a
        full poll interval late."""
        monkeypatch.setattr(
            settings, "direct_database_url", "postgresql://u:p@direct/db"
        )
        monkeypatch.setattr(settings, "database_url", "postgresql://u:p@pooled/db")

        assert "direct" in listener_dsn()

    def test_falls_back_to_the_pooled_url(self, monkeypatch) -> None:
        """A local stack has one endpoint, so both names point at it."""
        monkeypatch.setattr(settings, "direct_database_url", None)
        monkeypatch.setattr(settings, "database_url", "postgresql://u:p@only/db")

        assert "only" in listener_dsn()

    def test_strips_libpq_only_parameters(self, monkeypatch) -> None:
        """asyncpg rejects ``sslmode``; a URL copied from the Neon console has
        it. Dropping it does not disable TLS — asyncpg negotiates by default."""
        monkeypatch.setattr(
            settings,
            "direct_database_url",
            "postgresql://u:p@h/db?sslmode=require&channel_binding=require",
        )

        dsn = listener_dsn()
        assert "sslmode" not in dsn
        assert "channel_binding" not in dsn


class TestNotifyIsBestEffort:
    async def test_a_failing_notify_does_not_raise(
        self, db: AsyncSession, monkeypatch
    ) -> None:
        """The message is already durable when this runs. Raising here would
        fail a request that had successfully committed, trading a guaranteed
        delivery for a lost one."""

        async def _boom(*args, **kwargs):
            raise RuntimeError("channel is gone")

        monkeypatch.setattr(db, "execute", _boom)

        await notify_wakeup(db)  # must not raise


class TestListenerDegradesGracefully:
    async def test_an_unreachable_server_falls_back_to_polling(
        self, monkeypatch
    ) -> None:
        """A worker that cannot LISTEN is slower. One that refuses to start
        delivers nothing at all, so connection failure must not be fatal."""
        monkeypatch.setattr(
            settings, "direct_database_url", "postgresql://u:p@127.0.0.1:1/db"
        )

        async with WakeupListener() as listener:
            woken = await listener.wait(timeout=0.05)

        assert woken is False, "a disabled listener reports a timeout, not a wake"

    async def test_the_wait_is_always_bounded(self, monkeypatch) -> None:
        """The timeout is the liveness check, not just a fallback: a listener
        whose socket was silently dropped never fires, so an unbounded wait
        would block forever while the worker looked healthy."""
        monkeypatch.setattr(
            settings, "direct_database_url", "postgresql://u:p@127.0.0.1:1/db"
        )

        async with WakeupListener() as listener:
            await asyncio.wait_for(listener.wait(timeout=0.05), timeout=2.0)

    async def test_a_notification_wakes_the_waiter(self, monkeypatch) -> None:
        """The listener's own signalling, with the connection stubbed out.

        ``wait`` clears the event before sleeping — otherwise one notification
        would satisfy every future wait and the worker would spin. So the ping
        has to arrive *while* it is waiting, which is what this arranges.
        """
        monkeypatch.setattr(
            settings, "direct_database_url", "postgresql://u:p@127.0.0.1:1/db"
        )

        async with WakeupListener() as listener:
            listener._event = asyncio.Event()

            async def _ping() -> None:
                await asyncio.sleep(0.01)
                listener._on_notify()

            # Held, not fire-and-forget: asyncio keeps only a weak reference,
            # so an unreferenced task can be collected before it runs.
            ping = asyncio.ensure_future(_ping())
            try:
                assert await listener.wait(timeout=2.0) is True
            finally:
                await ping

    async def test_a_ping_during_processing_is_not_lost(self, monkeypatch) -> None:
        """A NOTIFY arriving while the worker is mid-batch must still be
        honoured. Clearing the event *before* waiting would discard it, and the
        worker would sleep the full interval with work already queued."""
        monkeypatch.setattr(
            settings, "direct_database_url", "postgresql://u:p@127.0.0.1:1/db"
        )

        async with WakeupListener() as listener:
            listener._event = asyncio.Event()
            listener._on_notify()  # arrives while "busy"

            assert await listener.wait(timeout=0.05) is True

    async def test_a_consumed_ping_does_not_wake_twice(self, monkeypatch) -> None:
        """Once honoured it is cleared, so the next idle wait respects the poll
        floor instead of spinning."""
        monkeypatch.setattr(
            settings, "direct_database_url", "postgresql://u:p@127.0.0.1:1/db"
        )

        async with WakeupListener() as listener:
            listener._event = asyncio.Event()
            listener._on_notify()

            assert await listener.wait(timeout=0.05) is True
            assert await listener.wait(timeout=0.05) is False


class TestTheChannelName:
    def test_the_channel_carries_no_payload(self) -> None:
        """Postgres caps a NOTIFY payload at 8000 bytes and raises *inside the
        committing transaction* if exceeded — so a large event payload would
        roll back the business write it was announcing. The ping carries nothing;
        the worker re-reads the table."""
        assert WAKEUP_CHANNEL.isidentifier(), (
            "the channel name is interpolated into SQL, so it must be a bare "
            "identifier and never user input"
        )
