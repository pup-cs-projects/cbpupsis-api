"""Waking the worker the moment a message is staged, instead of polling for it.

Polling alone is correct but wasteful: an empty claim query measured ~47ms
against a remote Neon instance, so a one-second interval is ~86,400 queries a day
doing nothing — and it keeps a connection busy enough that scale-to-zero never
triggers. A ``NOTIFY`` after commit lets the worker sleep until there is work,
and the poll interval drops to a safety floor.

Three things about Postgres LISTEN/NOTIFY that fail *quietly* if ignored, all
verified against a real server rather than assumed:

**The payload cap is 8000 bytes, and exceeding it raises inside the committing
transaction.** A fat event payload would not merely fail to notify — it would
roll back the business write it was announcing. So the notification carries no
data at all: it is a bare ping on :data:`WAKEUP_CHANNEL`, and the worker responds
by running its ordinary claim query. The payload never leaves the table.

**LISTEN needs a session-mode connection.** Neon's pooled URL is PgBouncer in
transaction mode and silently will not carry it, so the listener uses
``direct_database_url``. Symptom if missed: notifications work but are always a
full poll interval late, which reads like a tuning problem rather than a wrong
connection string.

**A LISTEN connection can die without telling you.** Firewalls and NAT gateways
drop idle TCP without sending anything the client sees, so a naive listener
blocks forever on a socket that is gone while looking perfectly healthy. The wait
is therefore always bounded by a timeout — the poll floor is not just a safety
net for missed notifications, it is the liveness check on the listener itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_shared.outbox.constants import WAKEUP_CHANNEL

logger = logging.getLogger(__name__)

#: libpq parameters asyncpg rejects when connecting directly. The same list
#: ``cbpupsis_database.session`` strips, for the same reason — a URL copied from
#: the Neon console carries them.
_LIBPQ_ONLY = frozenset(
    {"sslmode", "channel_binding", "target_session_attrs", "connect_timeout"}
)


def listener_dsn() -> str:
    """Return a plain libpq DSN for the listener connection.

    Uses the **direct** URL: the pooled one is PgBouncer in transaction mode and
    does not carry ``LISTEN``. Falls back to the pooled URL only so a local
    stack with one endpoint still works — there, both are the same server.
    """
    url = settings.direct_database_url or settings.database_url
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query) if k not in _LIBPQ_ONLY]
    return urlunsplit(("postgresql", parts.netloc, parts.path, urlencode(kept), ""))


async def notify_wakeup(db: AsyncSession) -> None:
    """Ping the worker that something is waiting.

    Best-effort and deliberately swallowing failures: a missed wake-up costs one
    poll interval of latency, while an exception here would fail the request that
    just successfully committed. The message is already durable at this point —
    that is the whole property the outbox provides — so the wake-up is an
    optimisation, never the delivery mechanism.

    Call it **after** the caller's commit, not before: notifying about a
    transaction that then rolls back wakes a worker to find nothing.
    """
    # NOTIFY is Postgres-only. Skipping it elsewhere rather than letting it fail
    # keeps the SQLite test suite honest: a failed statement leaves the session
    # in an aborted transaction, so the *next* operation fails with an error
    # naming neither NOTIFY nor the dialect.
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return

    try:
        await db.execute(text(f"NOTIFY {WAKEUP_CHANNEL}"))
        await db.commit()
    except Exception:  # a missed wake-up is a latency cost, not a failure
        logger.debug("Outbox wake-up notification failed", exc_info=True)
        # Rolled back, not just logged: an aborted transaction poisons the
        # session, and the caller — which has already committed its real work —
        # would fail on its next statement for a reason that looks unrelated.
        await db.rollback()


class WakeupListener:
    """Waits for a wake-up ping, with the poll interval as a hard ceiling.

    Used as an async context manager by the worker::

        async with WakeupListener() as listener:
            while running:
                await process()
                await listener.wait(timeout=poll_interval)

    :meth:`wait` returns whether a notification actually arrived, but the caller
    does not need to care: on a timeout it simply polls, which is what makes a
    silently dead listener degrade into "slower" rather than "stopped".

    Connecting is best-effort. If the direct URL is unreachable — or the server
    is behind a pooler that cannot LISTEN — the listener stays disabled and
    :meth:`wait` becomes a plain sleep. The worker keeps working; it just loses
    the latency improvement.
    """

    def __init__(self) -> None:
        self._connection = None
        self._event: asyncio.Event | None = None

    async def __aenter__(self) -> WakeupListener:
        self._event = asyncio.Event()
        try:
            self._connection = await asyncpg.connect(listener_dsn())
            await self._connection.add_listener(WAKEUP_CHANNEL, self._on_notify)
            logger.info("outbox.listener.connected", extra={"channel": WAKEUP_CHANNEL})
        except Exception:
            # Degrade to polling rather than refusing to start. A worker that
            # cannot LISTEN is slower; one that will not run delivers nothing.
            self._connection = None
            logger.warning(
                "outbox.listener.unavailable; falling back to polling",
                exc_info=True,
            )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._connection is not None:
            with contextlib.suppress(Exception):
                await self._connection.close()
            self._connection = None

    def _on_notify(self, *_args: object) -> None:
        """asyncpg calls this from the connection's reader task."""
        if self._event is not None:
            self._event.set()

    async def wait(self, timeout: float) -> bool:
        """Block until woken or ``timeout`` elapses. Returns whether woken.

        The timeout is not optional and not merely a fallback: it is what proves
        the connection is still alive. A listener whose socket was silently
        dropped never fires, so without a bound the worker would wait forever
        while looking healthy.
        """
        if self._event is None:
            await asyncio.sleep(timeout)
            return False

        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except TimeoutError:
            return False
        finally:
            # Cleared AFTER the wait, not before it. A ping that arrives while
            # the worker is mid-batch would otherwise be discarded, and the
            # worker would then sleep the full interval with work already
            # queued. Clearing here means such a ping is still honoured: the
            # next wait returns immediately, the worker claims, finds the row,
            # and the redundant extra pass costs one indexed query.
            self._event.clear()
        return True
