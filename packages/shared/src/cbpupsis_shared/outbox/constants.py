"""Fixed values for the transactional outbox.

The status vocabulary and the column lengths are defined beside the columns in
``cbpupsis_database.models.outbox``; ``OutboxStatus`` is re-exported here for
the store, the worker, and the scripts.
"""

from __future__ import annotations

from cbpupsis_database.models.outbox import (
    OutboxStatus as OutboxStatus,
)

#: How much of a failure's text is kept. Enough to identify the fault, bounded
#: so a pathological traceback cannot bloat the table it is stored in.
LAST_ERROR_MAX_LENGTH = 2000

#: The channel a bare wake-up ping is sent on. Carries NO payload: Postgres caps
#: NOTIFY at 8000 bytes and raises inside the committing transaction if it is
#: exceeded, so putting event data here would let a large payload roll back the
#: business write it was announcing.
WAKEUP_CHANNEL = "outbox_wakeup"
