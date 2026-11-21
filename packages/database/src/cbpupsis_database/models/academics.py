"""SQLAlchemy models for the academic calendar and terms domain."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from cbpupsis_database.base import Base, UUIDMixin


class AcademicTerm(UUIDMixin, Base):
    """Academic calendar term definition and enrollment windows."""

    __tablename__ = "academic_terms"

    academic_year: Mapped[str] = mapped_column(String(20))
    semester: Mapped[str] = mapped_column(String(30))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    is_current: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    early_enrollment_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    early_enrollment_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    regular_enrollment_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    regular_enrollment_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    late_enrollment_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    add_drop_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    grade_submission_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "academic_year", "semester", name="uq_academic_terms_year_sem"
        ),
    )


class AcademicEvent(UUIDMixin, Base):
    """Institutional academic calendar event."""

    __tablename__ = "academic_events"

    academic_term_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_terms.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, default=None)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date, default=None)
    event_type: Mapped[str] = mapped_column(String(50))

    __table_args__ = (Index("idx_fk_academic_events_term_id", "academic_term_id"),)
