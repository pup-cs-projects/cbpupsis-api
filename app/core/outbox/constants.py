"""Fixed values for the transactional outbox.

Separate from ``store.py`` so ``models.py`` can import lengths and the status
vocabulary without importing the query layer — the same split every domain makes
between ``constants.py`` and the rest.

Lengths are shared by the models and any schema built over them: a schema
admitting more than the column holds is a 500 at INSERT rather than a validation
error.
"""

from __future__ import annotations

from enum import StrEnum

EVENT_NAME_MAX_LENGTH = 100
STATUS_MAX_LENGTH = 20
CHANNEL_MAX_LENGTH = 32

#: How much of a failure's text is kept. Enough to identify the fault, bounded
#: so a pathological traceback cannot bloat the table it is stored in.
LAST_ERROR_MAX_LENGTH = 2000

#: The channel a bare wake-up ping is sent on. Carries NO payload: Postgres caps
#: NOTIFY at 8000 bytes and raises inside the committing transaction if it is
#: exceeded, so putting event data here would let a large payload roll back the
#: business write it was announcing.
WAKEUP_CHANNEL = "outbox_wakeup"


class OutboxStatus(StrEnum):
    """Lifecycle of an outbox message.

    A ``StrEnum`` so it compares equal to the plain strings stored in the column
    — the column is deliberately a VARCHAR rather than a database enum, so that
    adding a state is a code change rather than a migration that has to be
    coordinated with a deploy.
    """

    #: Staged and awaiting delivery. The only status the claim query looks at.
    PENDING = "pending"
    #: Delivered. Kept for a retention window, then pruned.
    DISPATCHED = "dispatched"
    #: Exhausted ``outbox_max_attempts``. This is the dead-letter state: rows
    #: stay for inspection rather than moving to a separate table, because the
    #: only operations needed are "list failed" and "reset to pending", both of
    #: which are a WHERE clause.
    FAILED = "failed"
