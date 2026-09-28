"""Wire the audit domain to the durable outbox worker.

Kept in its own module, and called once from ``main.py``, so that importing the
audit service has no side effect. A ``subscribe()`` executed at import time
registers a handler again on every re-import — which in a test suite means one
event producing N rows, and the count depending on collection order.
"""

from __future__ import annotations

import uuid

from app import worker
from app.core.events import Event
from app.domains.audit import service
from app.domains.audit.constants import AUDITED_EVENTS


async def _handle(event: Event, *, message_id: uuid.UUID) -> None:
    """Adapter matching the worker's idempotent handler signature."""
    await service.record_event(event, message_id=message_id)


def register_audit_subscribers() -> None:
    """Register the audit recorder for every event worth keeping.

    Idempotent: re-registering the same handler for an event is skipped, so
    calling this twice (an app rebuilt in a test, a reload in development) does
    not double every audit row.
    """
    for event_name in AUDITED_EVENTS:
        worker.register_handler(event_name, _handle)
