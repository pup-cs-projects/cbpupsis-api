"""The outbox worker: an entry point beside the API processes.

Same image, different command — ``python -m cbpupsis_shared.worker`` instead of
uvicorn. It claims messages the API staged, dispatches them to handlers, and
records the outcome. Nothing here serves HTTP.

Thin by design, like the app entry points: wiring and a loop. Every rule lives in
``cbpupsis_shared.outbox`` or in the handler being dispatched to.

Three decisions worth understanding before changing anything here.

**Claim, commit, then dispatch.** The claim transaction counts the attempt and
pushes the retry out, and it is committed *before* the outbound call. Holding the
row lock across a slow SES round trip would keep a transaction open for its whole
duration, and a crash mid-send would roll the attempt counter back — producing an
infinite retry loop on a poison message. Committing first means a crash leaves
the message backed off and eventually dead-lettered.

**Handlers are called directly, not through the event bus.** ``EventBus._run``
deliberately swallows handler exceptions so one broken listener cannot break an
unrelated flow. That is correct for fire-and-forget, and exactly wrong here: the
relay must learn that a delivery failed, or it marks undelivered messages as
dispatched and the durability guarantee silently evaporates. This is the single
most likely thing for a later reader to "simplify" back into a bug.

**Errors inside a message are caught; errors in the loop are not.** A worker that
catches everything and keeps looping while doing nothing is invisible — it looks
healthy by every measure an orchestrator can see. Letting loop-level failures
kill the process means ECS restarts it, which is the behaviour you want.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_core.events import Event
from cbpupsis_core.logging import configure_logging
from cbpupsis_database.session import AsyncSessionLocal, engine
from cbpupsis_shared.outbox import (
    OutboxMessage,
    WakeupListener,
    claim_pending,
    mark_dispatched,
    mark_failed,
    pending_stats,
    start_attempt,
)

# Before anything else, as create_app does: a logger created before
# configuration keeps the default handler and ignores everything set up here.
configure_logging()

logger = logging.getLogger(__name__)

#: Registered handlers, by event name. Populated by :func:`register_handler`.
#:
#: Deliberately separate from ``event_bus``: these run in the worker process,
#: are awaited, and are allowed to raise. A bus subscriber is fire-and-forget
#: and cannot report failure, which is the property this dispatch table exists
#: to avoid inheriting.
_HANDLERS: dict[str, list] = {}


def register_handler(event_name: str, handler) -> None:
    """Register ``handler`` to run when a message of ``event_name`` is claimed.

    Idempotent: registering the same handler twice is a no-op, so a module
    imported more than once cannot make one message deliver twice. The same
    guard ``audit/subscribers.py`` uses, and load-bearing for the same reason.
    """
    handlers = _HANDLERS.setdefault(event_name, [])
    if handler not in handlers:
        handlers.append(handler)


def handlers_for(event_name: str) -> tuple:
    """Return the handlers registered for ``event_name``."""
    return tuple(_HANDLERS.get(event_name, []))


async def dispatch(message: OutboxMessage) -> None:
    """Run every handler for one message, letting failures propagate.

    Propagation is the point. The caller turns an exception into a retry, so a
    handler that swallows its own errors — or a dispatcher that swallowed
    them — would mark the message delivered when it was not.

    A message with no registered handler is **not** an error: an event may be
    emitted before anything subscribes to it (``item.created`` is in exactly
    that state today). It is logged and marked dispatched, because retrying it
    forever would fill the backlog with work nobody wants done.
    """
    handlers = handlers_for(message.event_name)
    if not handlers:
        logger.info(
            "No handler for outbox message; discarding",
            extra={"event_name": message.event_name, "message_id": str(message.id)},
        )
        return

    event = Event(name=message.event_name, payload=dict(message.payload))
    for handler in handlers:
        await handler(event, message_id=message.id)


async def process_batch(db: AsyncSession) -> int:
    """Claim one batch, deliver it, and record each outcome. Returns the count.

    The session is the caller's so a test can drive a single batch against the
    same in-memory database it set up — the seam that makes the relay testable
    without a live worker process.
    """
    messages = await claim_pending(db)
    if not messages:
        return 0

    for message in messages:
        start_attempt(message)
    # Committed BEFORE dispatch: see the module docstring.
    await db.commit()

    for message in messages:
        try:
            await dispatch(message)
        except Exception as exc:
            mark_failed(message, exc)
            logger.warning(
                "Outbox delivery failed",
                extra={
                    "event_name": message.event_name,
                    "message_id": str(message.id),
                    "attempts": message.attempts,
                    "status": message.status,
                },
            )
        else:
            mark_dispatched(message)
        await db.commit()

    return len(messages)


async def _tick() -> int:
    """One poll: process a batch and emit the heartbeat. Its own session.

    A fresh session per tick rather than one held for the worker's lifetime: a
    long-lived session accumulates identity-map state and holds a pooled
    connection open, which on Neon counts against the connection limit for as
    long as the process runs.
    """
    async with AsyncSessionLocal() as db:
        claimed = await process_batch(db)
        stats = await pending_stats(db)

    # The heartbeat. `oldest_pending_seconds` is the field worth alerting on: a
    # worker wedged on a poison message keeps running and keeps logging, and
    # only the age of the backlog reveals it. log_json promotes these to
    # top-level keys, so they are queryable rather than a substring.
    logger.info("outbox.tick", extra={"claimed": claimed, **stats})
    return claimed


def _install_signal_handlers(shutdown: asyncio.Event) -> None:
    """Ask the loop to set ``shutdown`` on SIGTERM or SIGINT.

    ``loop.add_signal_handler`` rather than ``signal.signal``: the latter can
    fire in the middle of an await and is not async-safe.

    It only sets a flag. Cancelling mid-dispatch is precisely how you manufacture
    the duplicate the receipts exist to prevent, so the in-flight batch always
    finishes.

    Not available on Windows, where ``add_signal_handler`` raises
    ``NotImplementedError``. The worker runs in Linux containers; local Windows
    development falls back to Ctrl+C raising KeyboardInterrupt, which the caller
    handles.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, shutdown.set)


async def _wait_for_work(listener: WakeupListener, shutdown: asyncio.Event) -> None:
    """Sleep until there is work, the poll floor expires, or we are shutting down.

    Both waits run concurrently and the loser is cancelled, so a SIGTERM during
    an idle window is noticed immediately rather than after the poll interval.
    """
    waiters = [
        asyncio.ensure_future(
            listener.wait(timeout=settings.outbox_poll_interval_seconds)
        ),
        asyncio.ensure_future(shutdown.wait()),
    ]
    done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    for task in done:
        task.exception()  # retrieve, so a failure is not reported at GC time


async def run() -> None:
    """The worker loop. Runs until SIGTERM, then finishes cleanly."""
    # Imported here rather than at module scope: subscribers.py imports this
    # module to register handlers, so a top-level import would be circular.
    from cbpupsis_shared.domains.audit.subscribers import (  # noqa: PLC0415
        register_audit_subscribers,
    )
    from cbpupsis_shared.domains.notifications.subscribers import (  # noqa: PLC0415
        register_notification_subscribers,
    )

    register_audit_subscribers()
    register_notification_subscribers()

    shutdown = asyncio.Event()
    _install_signal_handlers(shutdown)

    logger.info(
        "outbox.worker.started",
        extra={
            "poll_interval": settings.outbox_poll_interval_seconds,
            "batch_size": settings.outbox_batch_size,
        },
    )
    try:
        async with WakeupListener() as listener:
            while not shutdown.is_set():
                claimed = await _tick()
                if claimed:
                    # A full batch probably means more is waiting; loop
                    # immediately rather than sleeping through a backlog.
                    continue
                # Sleep until a NOTIFY arrives, the poll floor elapses, or
                # shutdown is requested — whichever comes first.
                #
                # The floor is not merely a fallback for a missed NOTIFY: it is
                # what proves the listener's socket is still alive, since a
                # connection dropped by a firewall never fires and would
                # otherwise block here forever.
                #
                # Racing shutdown against it is what keeps SIGTERM responsive.
                # Waiting on the listener alone would leave a draining task idle
                # for the whole interval and eventually SIGKILLed.
                await _wait_for_work(listener, shutdown)
    finally:
        logger.info("outbox.worker.stopping")
        await engine.dispose()


def main() -> None:
    """Entry point for ``python -m cbpupsis_shared.worker``."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    # Delegate to the *imported* module rather than calling main() directly.
    #
    # `python -m cbpupsis_shared.worker` executes this file as `__main__`, and
    # anything that later does `from cbpupsis_shared import worker` imports it a
    # SECOND time under its real name. Two module objects, two `_HANDLERS`
    # dicts — so subscribers register into one while the loop reads the other,
    # and every message is discarded as "no handler" while the worker looks
    # perfectly healthy.
    #
    # Found exactly that way: the running worker logged `[__main__]`, not
    # `[cbpupsis_shared.worker]`. Importing here forces both names onto one object.
    from cbpupsis_shared.worker import main as _main

    _main()
