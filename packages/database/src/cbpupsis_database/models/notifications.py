"""SQLAlchemy models for the notifications domain.

Two tables: what a user was told, and how they want to be told. Both key on a
bare ``user_id`` UUID with **no foreign key** to ``users.id`` — the same rule
``items.owner_id`` and the IAM join tables follow, because a database-level
constraint across domains is a coupling a service split cannot sever.

Only this domain reads or writes these tables; other domains go through
``client.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from cbpupsis_database.base import Base, TimestampMixin, UUIDMixin

TITLE_MAX_LENGTH = 200
TYPE_MAX_LENGTH = 100

#: JSONB on Postgres, plain JSON elsewhere — the variant the audit and outbox
#: tables use, and for the same reason: the suite runs on SQLite, which has no
#: JSONB and cannot compile the type at all.
_JSON_TYPE = JSONB().with_variant(JSON(), "sqlite")


class Notification(UUIDMixin, TimestampMixin, Base):
    """One thing a user was told, and whether they have seen it.

    Unlike an audit entry this *is* updated — ``read_at`` is stamped when the
    user opens it — so it carries ``TimestampMixin`` in full.
    """

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=sa.text("gen_uuid_v7()")
    )

    #: Who it is for. Bare id, no FK: see the module docstring.
    user_id: Mapped[uuid.UUID] = mapped_column(index=True)

    #: The notification type, e.g. ``"item.created"``. A key into
    #: ``NOTIFICATION_TYPES`` rather than a database enum, so adding a type is a
    #: code change rather than a migration coordinated with a deploy.
    notification_type: Mapped[str] = mapped_column(String(TYPE_MAX_LENGTH))

    title: Mapped[str] = mapped_column(String(TITLE_MAX_LENGTH))
    body: Mapped[str] = mapped_column(Text)

    #: Structured detail the client renders — an id to link to, a count. Kept
    #: separate from the rendered body so a client can deep-link without parsing
    #: prose.
    payload: Mapped[dict[str, Any]] = mapped_column(_JSON_TYPE, default=dict)

    #: When the user opened it; NULL means unread. A timestamp rather than a
    #: boolean for the same reason as ``email_verified_at``: "when" is the
    #: question support and analytics ask, and it costs nothing extra to store.
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    #: The outbox message that produced this row. **This is the idempotency
    #: key**, and the UNIQUE constraint below is what makes at-least-once
    #: delivery safe: a redelivered message tries to insert a second row, the
    #: database refuses, and the handler treats that refusal as success.
    #:
    #: Nullable because a notification can also be created directly — by a
    #: script, or by a future in-app-only path — with no outbox message behind
    #: it. NULLs do not collide in a UNIQUE index, so those rows coexist freely.
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(default=None)

    __table_args__ = (
        # The dedupe. An application-level "check then insert" loses the race
        # that redelivery is most likely to create; a constraint does not.
        UniqueConstraint("source_message_id", name="uq_notification_source_message"),
        # The unread-count query — WHERE user_id = ? AND read_at IS NULL — runs
        # on every poll by every user, so it is the one query here that must
        # never scan. Partial on Postgres so the index carries only unread rows,
        # which is a small and roughly constant fraction of the table.
        Index(
            "ix_notification_unread",
            "user_id",
            "read_at",
            postgresql_where=(read_at.is_(None)),
        ),
    )


class NotificationPreference(UUIDMixin, TimestampMixin, Base):
    """One user's choice about one channel of one notification type.

    Rows exist only where a user has expressed a preference — **absence means
    the type's default**. That is what lets a new notification type apply
    immediately to everyone, instead of needing a backfill that would be wrong
    for every account created before the type existed.

    A row per (user, type, channel) rather than columns on ``users``: adding a
    type or a channel is then data, not a migration, which is the same argument
    that keeps IAM permissions in a table.
    """

    __tablename__ = "notification_preferences"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=sa.text("gen_uuid_v7()")
    )

    #: Bare id, no FK — see the module docstring.
    user_id: Mapped[uuid.UUID] = mapped_column(index=True)

    notification_type: Mapped[str] = mapped_column(String(TYPE_MAX_LENGTH))

    #: One of ``Channel``. A plain string for the same reason as
    #: ``notification_type``.
    channel: Mapped[str] = mapped_column(String(32))

    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa.true()
    )

    __table_args__ = (
        # One preference per user per type per channel. Without this, two
        # concurrent updates leave contradictory rows and the resolution order
        # decides which wins.
        UniqueConstraint(
            "user_id",
            "notification_type",
            "channel",
            name="uq_notification_preference",
        ),
    )


class DigestRun(UUIDMixin, Base):
    """One digest already staged for one user for one period.

    **The idempotency key for scheduled sends.** EventBridge retries a failed
    invocation, and a digest job that ran halfway would otherwise mail everyone
    it had already reached a second time. The UNIQUE constraint makes the retry
    a no-op for users already handled and lets it finish the rest.

    ``period_start`` is the UTC instant the digest covers from, so "the weekly
    digest of 2026-08-24" is one value regardless of the recipient's timezone.
    """

    __tablename__ = "digest_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=sa.text("gen_uuid_v7()")
    )

    #: Bare id, no FK — see the module docstring.
    user_id: Mapped[uuid.UUID] = mapped_column(index=True)

    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "period_start", name="uq_digest_run_period"),
    )


class CBPUPSISNotification(UUIDMixin, Base):
    """Domain notification entity mapped to cbpupsis_notifications."""

    __tablename__ = "cbpupsis_notifications"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(
        String(50), default="general", server_default="general"
    )
    link_url: Mapped[str | None] = mapped_column(String(255), default=None)
    is_read: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index(
            "idx_fk_cbpupsis_notifications_user_unread",
            "user_id",
            postgresql_where=sa.text("is_read = false"),
        ),
    )
