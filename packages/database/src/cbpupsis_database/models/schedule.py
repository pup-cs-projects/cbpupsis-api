"""Durable calendar revision and removed-event state."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from cbpupsis_database.base import Base


class ScheduleExportState(Base):
    """One serialized export stream per student and academic term."""

    __tablename__ = "schedule_export_states"
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    academic_term_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_terms.id", ondelete="CASCADE"), primary_key=True
    )
    fingerprint: Mapped[str] = mapped_column(String(64), default="", server_default="")
    sequence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    events: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
