"""The in-process event bus: background dispatch for side-effects safe to lose.

Moved here from the items tests when ``item.created`` moved to the outbox: these
pin the bus's own contract, which no longer depends on any domain.
"""

from __future__ import annotations

import asyncio

from cbpupsis_core.events import Event, EventBus

EVENT = Event(name="test.best_effort", payload={"n": 1})


async def test_a_subscriber_receives_the_event() -> None:
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe(EVENT.name, handler)
    await bus.publish(EVENT)
    # Dispatch is in the background; draining makes the assertion sound.
    await bus.drain()

    assert received == [EVENT]


async def test_publishing_does_not_wait_for_a_slow_handler() -> None:
    """A slow listener must not be added to the latency of the request."""
    bus = EventBus()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(event: Event) -> None:
        started.set()
        await release.wait()

    bus.subscribe(EVENT.name, slow)
    await bus.publish(EVENT)  # returns while the handler waits on release

    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    await bus.drain()


async def test_a_failing_handler_is_isolated_and_the_rest_still_run() -> None:
    bus = EventBus()
    received: list[Event] = []

    async def broken(event: Event) -> None:
        raise RuntimeError("handler is down")

    async def healthy(event: Event) -> None:
        received.append(event)

    bus.subscribe(EVENT.name, broken)
    bus.subscribe(EVENT.name, healthy)
    await bus.publish(EVENT)
    await bus.drain()

    assert received == [EVENT]
