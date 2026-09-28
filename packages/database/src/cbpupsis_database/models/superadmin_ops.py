"""Existing backup metadata used by two-person restore authorization."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from cbpupsis_database.base import Base, UUIDMixin


class SystemBackup(UUIDMixin, Base):
    __tablename__ = "system_backups"

    backup_type: Mapped[str] = mapped_column(String(20))
    byte_size: Mapped[int] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30))
    integrity_status: Mapped[str] = mapped_column(String(30), default="unverified")
    storage_location: Mapped[str] = mapped_column(Text)
    initiated_by: Mapped[uuid.UUID | None] = mapped_column()
    requested_restore_by: Mapped[uuid.UUID | None] = mapped_column()
    approved_restore_by: Mapped[uuid.UUID | None] = mapped_column()
    restore_status: Mapped[str | None] = mapped_column(String(30))
    restore_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    restore_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
