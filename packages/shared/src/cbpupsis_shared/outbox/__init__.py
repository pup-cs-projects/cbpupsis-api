"""The transactional outbox: side effects that survive a crash.

``event_bus.publish`` schedules handlers as ``asyncio`` tasks, so an event
announced just before a deploy, a crash, or a scale-in is simply gone. That is
the right trade for analytics and cache warming, and the wrong one for a receipt,
a security notice, or anything a user is told happened.

This package is the durable sibling. :func:`publish_transactional` stages a row
on the **caller's** session, so the event and the business change it describes
are one transaction: both commit or neither does. A worker then claims the row,
delivers it, and marks it done.

It lives beside the domains rather than inside one because every domain
publishes through it, the same reason they all depend on
``cbpupsis_core.events``. Putting it inside a feature domain would make every
other domain depend on that feature. It is in the shared package rather than
core because it queries the database, and core sits below the database.

Layout mirrors a domain's, minus the layers an infrastructure module has no use
for (no router, no schemas, no HTTP surface at all). Its tables live in
``cbpupsis_database.models.outbox`` with every other table::

    constants.py   the wake-up channel, error bound, re-exported status vocabulary
    store.py       the queries — staging, claiming, marking, receipts
    wakeup.py      LISTEN/NOTIFY, so the worker sleeps instead of polling

Import from the package, not the submodules::

    from cbpupsis_shared.outbox import publish_transactional

**Delivery is at-least-once, never exactly-once** — see ``store.py`` for why that
is unavoidable and what makes it safe.
"""

from __future__ import annotations

from cbpupsis_database.models.outbox import DeliveryReceipt, OutboxMessage
from cbpupsis_shared.outbox.constants import (
    WAKEUP_CHANNEL,
    OutboxStatus,
)
from cbpupsis_shared.outbox.store import (
    add_receipt,
    already_delivered,
    claim_pending,
    lock_message,
    mark_dispatched,
    mark_failed,
    pending_stats,
    publish_transactional,
    start_attempt,
)
from cbpupsis_shared.outbox.wakeup import WakeupListener, notify_wakeup

__all__ = [
    "WAKEUP_CHANNEL",
    "DeliveryReceipt",
    "OutboxMessage",
    "OutboxStatus",
    "WakeupListener",
    "add_receipt",
    "already_delivered",
    "claim_pending",
    "lock_message",
    "mark_dispatched",
    "mark_failed",
    "notify_wakeup",
    "pending_stats",
    "publish_transactional",
    "start_attempt",
]
