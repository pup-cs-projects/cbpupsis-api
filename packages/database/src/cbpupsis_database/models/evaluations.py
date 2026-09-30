"""SQLAlchemy models for faculty evaluations and evaluation tags domain."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from cbpupsis_database.base import Base, UUIDMixin

_JSON_TYPE = postgresql.JSONB(astext_type=Text()).with_variant(JSON(), "sqlite")


class FacultyEvaluationPeriod(UUIDMixin, Base):
    """Window during which students evaluate course faculty."""

    __tablename__ = "faculty_evaluation_periods"

    academic_term_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_terms.id", ondelete="CASCADE")
    )
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )


class FacultyEvaluation(UUIDMixin, Base):
    """Anonymized evaluation submission."""

    __tablename__ = "faculty_evaluations"

    evaluation_period_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("faculty_evaluation_periods.id", ondelete="CASCADE")
    )
    faculty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    section_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("course_sections.id", ondelete="CASCADE")
    )
    ratings: Mapped[dict[str, Any]] = mapped_column(_JSON_TYPE)
    comments: Mapped[str | None] = mapped_column(Text, default=None)
    submitted_at_coarsened: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_fk_faculty_evaluations_faculty_id", "faculty_id"),
        Index("idx_fk_faculty_evaluations_section_id", "section_id"),
    )


class FacultyEvaluationTag(UUIDMixin, Base):
    """Deduplication tracker guaranteeing single submission per student per section."""

    __tablename__ = "faculty_evaluation_tags"

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    section_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("course_sections.id", ondelete="CASCADE")
    )
    evaluation_period_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("faculty_evaluation_periods.id", ondelete="CASCADE")
    )
    has_submitted: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )

    __table_args__ = (
        UniqueConstraint(
            "student_id",
            "section_id",
            "evaluation_period_id",
            name="uq_faculty_eval_tag",
        ),
    )
