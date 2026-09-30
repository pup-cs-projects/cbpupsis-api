"""SQLAlchemy models for system health telemetry and database backup archives."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from cbpupsis_database.base import Base, UUIDMixin


class SystemHealthLog(Base):
    """System performance telemetry log partitioned by recorded_at."""

    __tablename__ = "system_health_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    api_response_time_ms: Mapped[int] = mapped_column(Integer)
    db_query_time_ms: Mapped[int] = mapped_column(Integer)
    active_users_count: Mapped[int] = mapped_column(Integer)
    cpu_utilization_pct: Mapped[Decimal] = mapped_column(Numeric(precision=5, scale=2))
    memory_utilization_pct: Mapped[Decimal] = mapped_column(
        Numeric(precision=5, scale=2)
    )
    disk_usage_pct: Mapped[Decimal] = mapped_column(Numeric(precision=5, scale=2))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        server_default=func.now(),
        nullable=False,
    )


class SystemBackup(UUIDMixin, Base):
    """Database backup archives with dual-custody approval for restore."""

    __tablename__ = "system_backups"

    backup_type: Mapped[str] = mapped_column(String(20))
    byte_size: Mapped[int] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    status: Mapped[str] = mapped_column(String(30))
    integrity_status: Mapped[str] = mapped_column(
        String(30), default="unverified", server_default="unverified"
    )
    storage_location: Mapped[str] = mapped_column(Text)
    initiated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    requested_restore_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    approved_restore_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    restore_status: Mapped[str | None] = mapped_column(String(30), default=None)
    restore_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    restore_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
