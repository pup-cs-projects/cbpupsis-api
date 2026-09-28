"""Wire the audit domain to the event bus.

Kept in its own module, and called by ``create_app`` in
``cbpupsis_shared.application``, so that importing the audit service has no side
effect. A ``subscribe()`` executed at import time
registers a handler again on every re-import — which in a test suite means one
event producing N rows, and the count depending on collection order.
"""

from __future__ import annotations

from cbpupsis_core.events import event_bus
from cbpupsis_shared.domains.audit import service
from cbpupsis_shared.domains.audit.constants import AUDITED_EVENTS


def register_audit_subscribers() -> None:
    """Subscribe the audit recorder to every event worth keeping.

    Idempotent: re-registering the same handler for an event is skipped, so
    calling this twice (an app rebuilt in a test, a reload in development) does
    not double every audit row.
    """
    for event_name in AUDITED_EVENTS:
        if service.record_event not in event_bus.handlers_for(event_name):
            event_bus.subscribe(event_name, service.record_event)
