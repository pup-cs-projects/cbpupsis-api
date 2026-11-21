"""Wire the notifications domain to the outbox worker.

Kept in its own module, and called once from ``cbpupsis_shared.worker``, so that
importing the notifications service has no side effect. A ``register_handler``
executed at import time runs again on every re-import — which in a test suite
means one message producing N notifications, with the count depending on
collection order.
"""

from __future__ import annotations

from cbpupsis_shared import worker
from cbpupsis_shared.domains.notifications import handlers
from cbpupsis_shared.domains.notifications.constants import NOTIFIED_EVENTS


def register_notification_subscribers() -> None:
    """Subscribe the notification handler to every event worth notifying on.

    Idempotent: the worker's registry skips a handler it already holds, so
    calling this twice — an app rebuilt in a test, a reload in development —
    cannot double every notification.
    """
    for event_name in NOTIFIED_EVENTS:
        worker.register_handler(event_name, handlers.deliver_event)
