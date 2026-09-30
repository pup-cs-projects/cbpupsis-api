"""SQLAlchemy models for enrollment, add/drop, overload requests, and COR issuances."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from cbpupsis_database.base import Base, UUIDMixin


class TermEnrollment(UUIDMixin, Base):
    """Official term-level student enrollment record."""

    __tablename__ = "term_enrollments"

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    academic_term_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_terms.id", ondelete="CASCADE")
    )
    enrollment_status: Mapped[str] = mapped_column(
        String(30), default="enrolled", server_default="enrolled"
    )
    cor_downloaded: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "student_id",
            "academic_term_id",
            name="uq_term_enrollments_student_term",
        ),
        UniqueConstraint("id", "academic_term_id", name="uq_term_enrollments_id_term"),
        Index("idx_fk_term_enrollments_term_id", "academic_term_id"),
    )


class CorIssuance(UUIDMixin, Base):
    """Cryptographically verifiable Certificate of Registration issuance."""

    __tablename__ = "cor_issuances"

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    term_enrollment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("term_enrollments.id", ondelete="CASCADE")
    )
    verification_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(
        String(20), default="CURRENT", server_default="CURRENT"
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    __table_args__ = (
        Index("idx_fk_cor_issuances_enrollment_id", "term_enrollment_id"),
    )


class StudentCourseEnrollment(UUIDMixin, Base):
    """Course section registration for an enrolled student."""

    __tablename__ = "student_course_enrollments"

    term_enrollment_id: Mapped[uuid.UUID] = mapped_column()
    academic_term_id: Mapped[uuid.UUID] = mapped_column()
    section_id: Mapped[uuid.UUID] = mapped_column()
    status: Mapped[str] = mapped_column(
        String(20), default="enrolled", server_default="enrolled"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["section_id", "academic_term_id"],
            ["course_sections.id", "course_sections.academic_term_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["term_enrollment_id", "academic_term_id"],
            ["term_enrollments.id", "term_enrollments.academic_term_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "term_enrollment_id",
            "section_id",
            name="uq_student_course_enrollments_item",
        ),
        Index("idx_fk_student_course_enrollments_section_id", "section_id"),
        Index(
            "idx_student_course_enrollments_active_seats",
            "section_id",
            postgresql_where=sa.text("status = 'enrolled'"),
        ),
    )


class AddDropRequest(UUIDMixin, Base):
    """Student request to add, drop, or swap course sections."""

    __tablename__ = "add_drop_requests"

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    section_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("course_sections.id", ondelete="CASCADE")
    )
    request_type: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(100))
    comments: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(
        String(20), default="submitted", server_default="submitted"
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    review_notes: Mapped[str | None] = mapped_column(Text, default=None)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OverloadRequest(UUIDMixin, Base):
    """Graduating student request to exceed term unit limit."""

    __tablename__ = "overload_requests"

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    academic_term_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_terms.id", ondelete="CASCADE")
    )
    requested_units: Mapped[int] = mapped_column(SmallInteger)
    justification: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(
        String(30), default="PENDING_DEAN", server_default="PENDING_DEAN"
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    review_remarks: Mapped[str | None] = mapped_column(Text, default=None)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("idx_fk_overload_requests_student_id", "student_id"),)


class CourseWaiver(UUIDMixin, Base):
    """Prerequisite course waiver granted to a student."""

    __tablename__ = "course_waivers"

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    section_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("course_sections.id", ondelete="CASCADE")
    )
    prerequisite_course_code: Mapped[str] = mapped_column(String(20))
    condition: Mapped[str] = mapped_column(String(50))
    justification: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(
        String(30), default="PENDING_CHAIR", server_default="PENDING_CHAIR"
    )
    chair_approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    chair_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    dean_approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    dean_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    vpaa_token: Mapped[str | None] = mapped_column(String(128), default=None)
    vpaa_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    review_remarks: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("idx_fk_course_waivers_student_id", "student_id"),)
