"""In-process event dispatcher — the async seam for future extraction.

Side-effects (send an email, update analytics, refresh a recommender) are
emitted as events rather than called inline, so the emitting domain never learns
who reacts. Today handlers run in-process; after a service split, this
dispatcher's implementation is swapped for a broker and neither emitters nor
handlers change.

A handler that raises is logged and skipped rather than propagating. A failing
analytics listener must not roll back the booking that triggered it — and once
this is a broker, the publisher is a separate process that could not see the
exception anyway, so isolating failures here keeps local behaviour honest about
what production will do.

**Dispatch is in the background.** ``publish`` schedules handlers and returns
without waiting, so a slow listener costs the caller nothing and the user's
response is not held open behind an email send. That is the right default, and
it is also a real weakening of the delivery guarantee, stated plainly because
the honest version is the argument for a broker later:

- A scheduled task dies with the process. A deploy, a crash, or a scale-in
  between the publish and the handler running loses the event with no record
  that it existed.
- There is no retry and no dead-letter queue. ``publish`` returning is not
  evidence that anything happened, only that it was queued.
- Ordering across two publishes is not guaranteed.

So this is fit for side-effects that may be lost — analytics, a cache refresh, a
nice-to-have notification. Anything that must not be lost (a payment capture, a
provisioning step) needs either a durable outbox in the same transaction as the
write, or a real broker. Both are deliberate additions; neither is what this is.

``publish_sync`` is the escape hatch for the case where a caller genuinely needs
the handlers to have finished — a test asserting on a side-effect, or a script
that exits immediately afterwards and would otherwise cancel its own work.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

Handler = Callable[["Event"], Awaitable[None]]


@dataclass(frozen=True)
class Event:
    """A named thing that happened, with a JSON-serialisable payload.

    Keep payloads to plain data (ids and scalars, never ORM objects): once a
    broker sits in the middle, anything richer cannot survive the trip.
    """

    name: str
    payload: dict[str, Any] = field(default_factory=dict)


class EventBus:
    """A minimal publish/subscribe dispatcher."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
        # Strong references to in-flight tasks. asyncio keeps only a weak one,
        # so a task nobody holds can be garbage-collected mid-await and vanish
        # silently — the handler simply never finishes, with nothing logged.
        self._tasks: set[asyncio.Task[None]] = set()

    def subscribe(self, event_name: str, handler: Handler) -> None:
        """Register ``handler`` to run when ``event_name`` is published."""
        self._handlers[event_name].append(handler)

    def handlers_for(self, event_name: str) -> tuple[Handler, ...]:
        """Return the handlers registered for ``event_name``.

        Exposed so a caller can register idempotently rather than reaching into
        ``_handlers``: subscribing the same handler twice would double every
        side-effect, and that is a plausible accident wherever an app is rebuilt
        (a test fixture, a development reload).
        """
        return tuple(self._handlers.get(event_name, []))

    async def publish(self, event: Event) -> None:
        """Schedule every subscribed handler and return without waiting.

        Stays ``async`` despite awaiting nothing: every call site already awaits
        it, and the broker version of this method genuinely will await a network
        round trip. Making it synchronous now would mean changing every emitter
        back later.
        """
        for handler in self._handlers.get(event.name, []):
            task = asyncio.create_task(self._run(handler, event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def publish_sync(self, event: Event) -> None:
        """Deliver ``event`` and wait for every handler to finish.

        For callers that must observe the side-effect: a test asserting on it,
        or a short-lived script whose exit would otherwise cancel the tasks
        ``publish`` scheduled. Failures are still isolated and logged.
        """
        for handler in self._handlers.get(event.name, []):
            await self._run(handler, event)

    async def _run(self, handler: Handler, event: Event) -> None:
        """Run one handler, routing any failure to the error sink.

        The explicit sink is what keeps background dispatch honest: a task whose
        exception nobody retrieves is reported only when the interpreter
        finalises it, long after the context that would explain it is gone.
        """
        try:
            await handler(event)
        except asyncio.CancelledError:
            # Shutdown, not a handler bug. Re-raised so cancellation still
            # propagates rather than being swallowed as a failure.
            raise
        except Exception:
            logger.exception(
                "Event handler %s failed for %s",
                getattr(handler, "__qualname__", handler),
                event.name,
            )

    async def drain(self) -> None:
        """Wait for currently scheduled handlers to finish.

        Called on shutdown so in-flight side-effects get a chance to complete
        rather than being cancelled mid-write. Best-effort by nature: it cannot
        recover anything already lost to a crash, which is the limitation the
        module docstring is about.
        """
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)


event_bus = EventBus()
