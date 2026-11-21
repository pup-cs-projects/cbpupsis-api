"""SQLAlchemy models for the course sections and schedules domain."""

from __future__ import annotations

import uuid
from datetime import time

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from cbpupsis_database.base import Base, UUIDMixin


class CourseSection(UUIDMixin, Base):
    """A scheduled section offering for a course in an academic term."""

    __tablename__ = "course_sections"

    academic_term_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_terms.id", ondelete="CASCADE")
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="RESTRICT")
    )
    faculty_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    section_name: Mapped[str] = mapped_column(String(30))
    capacity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(20), default="open", server_default="open"
    )

    __table_args__ = (
        UniqueConstraint(
            "academic_term_id",
            "course_id",
            "section_name",
            name="uq_course_sections_term_course_sec",
        ),
        UniqueConstraint("id", "academic_term_id", name="uq_course_sections_id_term"),
        Index("idx_fk_course_sections_course_id", "course_id"),
        Index("idx_fk_course_sections_faculty_id", "faculty_id"),
    )


class SectionSchedule(UUIDMixin, Base):
    """Weekly schedule slots and classroom for a course section."""

    __tablename__ = "section_schedules"

    section_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("course_sections.id", ondelete="CASCADE")
    )
    day_of_week: Mapped[int] = mapped_column(SmallInteger)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    room: Mapped[str | None] = mapped_column(String(50), default=None)
    schedule_type: Mapped[str] = mapped_column(
        String(20), default="lecture", server_default="lecture"
    )

    __table_args__ = (Index("idx_fk_section_schedules_section_id", "section_id"),)
