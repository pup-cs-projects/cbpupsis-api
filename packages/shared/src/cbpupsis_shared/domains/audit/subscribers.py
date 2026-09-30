"""Wire the audit domain to the durable outbox worker.

Kept in its own module and called by ``cbpupsis_shared.worker`` so importing the
audit service has no registration side effect.
"""

from __future__ import annotations

import uuid

from cbpupsis_core.events import Event
from cbpupsis_shared import worker
from cbpupsis_shared.domains.audit import service
from cbpupsis_shared.domains.audit.constants import AUDITED_EVENTS


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
