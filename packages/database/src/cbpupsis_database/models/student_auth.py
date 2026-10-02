"""SQLAlchemy models owned by the student-auth domain.

The table names are intentionally retained from the original implementation.
This is an ownership move, not a schema change: existing sessions and login
audit history remain readable without a data migration.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from cbpupsis_database.base import Base, UUIDMixin

_INET_TYPE = postgresql.INET().with_variant(String(45), "sqlite")


class UserActiveSession(UUIDMixin, Base):
    """A student login session used to enforce the idle timeout."""

    __tablename__ = "user_active_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    session_token_hash: Mapped[str] = mapped_column(String(64))
    device_info: Mapped[str] = mapped_column(Text)
    ip_address: Mapped[str] = mapped_column(_INET_TYPE)
    is_current: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("idx_fk_user_active_sessions_user_id", "user_id"),)


class AuthAuditLog(Base):
    """Append-only student authentication attempts, partitioned by time."""

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
