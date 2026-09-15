"""The transactional outbox: side effects that survive a crash.

``event_bus.publish`` schedules handlers as ``asyncio`` tasks, so an event
announced just before a deploy, a crash, or a scale-in is simply gone. That is
the right trade for analytics and cache warming, and the wrong one for a receipt,
a security notice, or anything a user is told happened.

This package is the durable sibling. :func:`publish_transactional` stages a row
on the **caller's** session, so the event and the business change it describes
are one transaction: both commit or neither does. A worker then claims the row,
delivers it, and marks it done.

It lives in ``core`` rather than a domain because every domain publishes through
it — ``audit``, ``iam``, ``items``, and ``users`` already depend on
``core.events`` for exactly the same reason. Putting it inside a feature domain
would make every other domain depend on that feature.

Layout mirrors a domain's, minus the layers an infrastructure module has no use
for (no router, no schemas, no HTTP surface at all)::

    constants.py   lengths, the status vocabulary, the wake-up channel
    models.py      OutboxMessage, DeliveryReceipt
    store.py       the queries — staging, claiming, marking, receipts
    wakeup.py      LISTEN/NOTIFY, so the worker sleeps instead of polling

Import from the package, not the submodules::

    from app.core.outbox import publish_transactional

**Delivery is at-least-once, never exactly-once** — see ``store.py`` for why that
is unavoidable and what makes it safe.
"""

from __future__ import annotations

from app.core.outbox.constants import (
    WAKEUP_CHANNEL,
    OutboxStatus,
)
from app.core.outbox.models import DeliveryReceipt, OutboxMessage
from app.core.outbox.store import (
    add_receipt,
    already_delivered,
    claim_pending,
    mark_dispatched,
    mark_failed,
    pending_stats,
    publish_transactional,
    start_attempt,
)
from app.core.outbox.wakeup import WakeupListener, notify_wakeup

__all__ = [
    "WAKEUP_CHANNEL",
    "DeliveryReceipt",
    "OutboxMessage",
    "OutboxStatus",
    "WakeupListener",
    "add_receipt",
    "already_delivered",
    "claim_pending",
    "mark_dispatched",
    "mark_failed",
    "notify_wakeup",
    "pending_stats",
    "publish_transactional",
    "start_attempt",
]
