"""SQLAlchemy models for the audit domain.

One append-only table recording administrative changes: who did what, to whom,
and when. Nothing updates or deletes a row here — an audit trail that can be
edited is not evidence of anything.

Why a table rather than only a log line: a log is retained for weeks and queried
by substring, while "when did this user get ManageIAM, and who granted it?" is
asked months later and joins to other data. Both happen — the handler logs *and*
persists — but only one of them survives log rotation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, String, Text, event, func
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from cbpupsis_database.base import Base, UUIDMixin

ACTION_MAX_LENGTH = 100
TARGET_TYPE_MAX_LENGTH = 50

#: JSONB on Postgres, plain JSON elsewhere. The test suite runs on SQLite, which
#: has no JSONB — without the variant every audit test fails on a type the
#: dialect cannot compile, and the production column would still be the one that
#: matters.
_JSON_TYPE = JSONB().with_variant(JSON(), "sqlite")


class AuditEntry(UUIDMixin, Base):
    """One recorded administrative change.

    Deliberately not ``TimestampMixin``: an audit row is never updated, so an
    ``updated_at`` on it would be a column that must always equal ``created_at``
    and quietly invites someone to write to it. ``occurred_at`` is named for
    when the thing happened rather than when the row was written, because after
    a retry or a replay through a broker those are not the same instant.
    """

    __tablename__ = "audit_entries"

    #: The event name, e.g. ``"iam.policy_attached_to_group"``. Stored as the
    #: event's own string rather than an enum: a new event type must not require
    #: a migration before it can be recorded, and an audit trail that silently
    #: drops unknown events is worse than one with an unfamiliar name in it.
    action: Mapped[str] = mapped_column(String(ACTION_MAX_LENGTH), index=True)

    #: Who performed it. Nullable because not every recorded change has a human
    #: behind it — a seed script or a migration has no actor, and inventing one
    #: would be a lie in the one table that must not contain them.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(default=None, index=True)

    #: What it was done to: the kind ("user", "group", "policy") and the id.
    #: A bare string id, not an FK — the target may live in another domain's
    #: table, and the audit trail must outlive the row it describes.
    target_type: Mapped[str | None] = mapped_column(
        String(TARGET_TYPE_MAX_LENGTH), default=None
    )
    target_id: Mapped[str | None] = mapped_column(String(255), default=None, index=True)

    #: The event payload verbatim, for the detail the columns above do not
    #: capture. JSONB so it stays queryable rather than becoming an opaque blob.
    payload: Mapped[dict[str, Any]] = mapped_column(_JSON_TYPE, default=dict)

    #: Explicit state snapshots make a change reconstructible without parsing
    #: action-specific payload shapes. ``None`` is valid for create/delete.
    prior_state: Mapped[dict[str, Any] | None] = mapped_column(_JSON_TYPE, default=None)
    new_state: Mapped[dict[str, Any] | None] = mapped_column(_JSON_TYPE, default=None)

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (
        # The two questions an audit trail is actually asked: "what happened to
        # this thing?" and "what has this person been doing?" Both are answered
        # newest-first, hence the descending time component.
        Index("ix_audit_target", "target_type", "target_id", "occurred_at"),
        Index("ix_audit_actor_time", "actor_id", "occurred_at"),
    )


@event.listens_for(AuditEntry, "before_update")
@event.listens_for(AuditEntry, "before_delete")
def _refuse_audit_mutation(_mapper, _connection, _target) -> None:
    """Fail ORM updates/deletes; the migration installs the same DB guard."""
    raise PermissionError("audit entries are append-only")


_INET_TYPE = postgresql.INET().with_variant(String(45), "sqlite")


class AuthAuditLog(Base):
    """Authentication audit log partitioned by created_at."""

    __tablename__ = "auth_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    attempted_id: Mapped[str] = mapped_column(String(50))
    ip_address: Mapped[str | None] = mapped_column(_INET_TYPE, default=None)
    user_agent: Mapped[str | None] = mapped_column(Text, default=None)
    success: Mapped[bool] = mapped_column(Boolean)
    failure_reason: Mapped[str | None] = mapped_column(String(100), default=None)
    is_suspicious: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    mfa_used: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        server_default=func.now(),
        nullable=False,
    )


class AdminAuditTrail(Base):
    """Administrative action audit trail partitioned by created_at."""

    __tablename__ = "admin_audit_trails"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column()
    module: Mapped[str] = mapped_column(String(50))
    action_type: Mapped[str] = mapped_column(String(50))
    target_entity: Mapped[str] = mapped_column(String(100))
    target_entity_id: Mapped[uuid.UUID] = mapped_column()
    previous_state: Mapped[dict[str, Any] | None] = mapped_column(
        _JSON_TYPE, default=None
    )
    updated_state: Mapped[dict[str, Any] | None] = mapped_column(
        _JSON_TYPE, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        server_default=func.now(),
        nullable=False,
    )
