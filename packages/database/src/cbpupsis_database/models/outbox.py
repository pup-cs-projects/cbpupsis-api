"""Tables backing the transactional outbox.

The queries over these tables live in ``cbpupsis_shared.outbox.store``; only
the table definitions live here, beside every other model, so ``models/__init__``
registers them with the rest.

**Delivery is at-least-once, not exactly-once.** See the docstring of
``cbpupsis_shared.outbox`` for why that is unavoidable, and its ``store.py`` for
what makes it safe.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from cbpupsis_database.base import Base, UUIDMixin

EVENT_NAME_MAX_LENGTH = 100
STATUS_MAX_LENGTH = 20
CHANNEL_MAX_LENGTH = 32


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


#: JSONB on Postgres, plain JSON elsewhere — the same variant the audit domain
#: uses, and for the same reason: the test suite runs on SQLite, which has no
#: JSONB, and without this every outbox test fails on a type the dialect cannot
#: compile.
_JSON_TYPE = JSONB().with_variant(JSON(), "sqlite")


class OutboxMessage(UUIDMixin, Base):
    """One durable side-effect, staged in the same transaction as its cause.

    Deliberately not ``TimestampMixin``: the meaningful timestamps here are
    domain-specific (``available_at``, ``dispatched_at``) and an ``updated_at``
    that changed on every retry would say nothing ``attempts`` does not already.
    ``created_at`` is declared explicitly because the age of the oldest pending
    row is the number worth alerting on.

    The ``id`` is the **idempotency key**. It is generated when the row is staged
    and is stable across every redelivery, so a handler can recognise work it has
    already done. That is what makes at-least-once delivery safe.
    """

    __tablename__ = "outbox_messages"

    #: The same string as ``Event.name``, so the outbox and the in-process bus
    #: share one vocabulary and a handler can be registered against either.
    event_name: Mapped[str] = mapped_column(String(EVENT_NAME_MAX_LENGTH))

    #: The event payload verbatim. It stays here and is never sent through
    #: ``NOTIFY``, whose payload is capped at 8000 bytes — exceeding it raises
    #: inside the committing transaction and would roll back the business write.
    payload: Mapped[dict[str, Any]] = mapped_column(_JSON_TYPE, default=dict)

    #: ``pending`` | ``dispatched`` | ``failed``. A string, not a database enum:
    #: adding a state must not require a migration coordinated with a deploy.
    #: Not separately indexed: ix_outbox_claimable below leads with this column,
    #: so a standalone index would be redundant write cost on every claim.
    status: Mapped[str] = mapped_column(
        String(STATUS_MAX_LENGTH), default=OutboxStatus.PENDING
    )

    #: When the row becomes claimable. Backoff is implemented by pushing this
    #: forward rather than by a separate scheduler, so a retry is just a row
    #: that is not yet due.
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    #: Incremented in the claim transaction, before dispatch. A crash mid-send
    #: therefore leaves it already incremented, so a poison message backs off and
    #: eventually dead-letters instead of retrying forever.
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    #: Truncated exception text from the last failure — the only clue an operator
    #: has about a dead-lettered row.
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # The claim query: WHERE status = 'pending' AND available_at <= now()
        # ORDER BY available_at. Partial on Postgres so the index never carries
        # the dispatched rows, which are the overwhelming majority over time.
        # postgresql_where is ignored by SQLite, so the test suite is unaffected.
        Index(
            "ix_outbox_claimable",
            "status",
            "available_at",
            postgresql_where=(status == OutboxStatus.PENDING),
        ),
    )


class DeliveryReceipt(UUIDMixin, Base):
    """Proof that one message was delivered on one channel. The email dedupe.

    ``send_email`` has no idempotency of its own — call it twice and the
    recipient gets two messages. Since redelivery is a normal part of
    at-least-once, something has to remember that a send already happened, and a
    row with a UNIQUE constraint is the only version of that which survives a
    crash.

    The channel writes this row **before** calling out, so the residual failure
    window is "receipt committed, process died before the send" — which loses a
    message rather than duplicating it. The message is still ``pending``, so the
    next attempt retries it. Losing-then-retrying is the correct direction to
    fail in; duplicating a password-reset email is not.
    """

    __tablename__ = "delivery_receipts"

    #: The outbox message this delivery satisfies. A bare UUID with no foreign
    #: key: receipts are pruned on a different schedule than the messages they
    #: guard, and a cascade would delete the very record that prevents a
    #: duplicate.
    outbox_message_id: Mapped[uuid.UUID] = mapped_column(index=True)

    #: ``email`` | ``in_app`` | ... — one receipt per channel, because a message
    #: delivered by email may still be pending in-app.
    channel: Mapped[str] = mapped_column(String(CHANNEL_MAX_LENGTH))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # The constraint IS the mechanism. Application-level "check then insert"
        # loses the race that redelivery is most likely to create.
        UniqueConstraint(
            "outbox_message_id", "channel", name="uq_delivery_receipt_message_channel"
        ),
    )
