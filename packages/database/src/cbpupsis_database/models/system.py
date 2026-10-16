"""SQLAlchemy models for system health telemetry."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    Integer,
    Numeric,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from cbpupsis_database.base import Base


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
