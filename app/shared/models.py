"""Reusable SQLAlchemy model mixins.

These mixins are deliberately domain-agnostic: they encode structural
conventions (surrogate keys, audit timestamps, soft deletion) that almost
every table in almost every web application wants. Compose them into concrete
models via multiple inheritance, for example::

    class User(UUIDMixin, TimestampMixin, Base):
        __tablename__ = "users"
        ...

Keeping these in ``app/shared`` rather than inside a single domain signals that
they are cross-cutting infrastructure, safe to reuse across—and copy out of—this
project.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column


class UUIDMixin:
    """Adds a UUID surrogate primary key.

    UUID keys are preferred over auto-incrementing integers here because they
    are unguessable, can be generated client-side, and do not collide when data
    from independently deployed services is merged—useful if a domain is later
    extracted into its own service.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )


class TimestampMixin:
    """Adds ``created_at`` and ``updated_at`` audit columns.

    Both timestamps are timezone-aware and populated by the database server
    (via ``func.now()``) so their values are consistent regardless of the
    application server's local clock. ``updated_at`` is refreshed automatically
    on every UPDATE through ``onupdate``.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    """Adds a nullable ``deleted_at`` column for soft deletion.

    Rather than issuing a physical DELETE, application code sets ``deleted_at``
    to the current time. This preserves the row for audit trails and keeps
    referential integrity intact for records that point at it. Queries that
    should exclude soft-deleted rows must filter on ``deleted_at IS NULL``;
    encapsulate that filter in the owning domain's service layer so callers do
    not have to remember it.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )
