"""SQLAlchemy models for grades, assessments, INC completions, and standing appeals."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from cbpupsis_database.base import Base, UUIDMixin


class StudentGrade(UUIDMixin, Base):
    """Final term grade for a student course enrollment."""

    __tablename__ = "student_grades"

    enrollment_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student_course_enrollments.id", ondelete="CASCADE"),
        unique=True,
    )
    raw_score: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=5, scale=2), default=None
    )
    midterm_grade: Mapped[str | None] = mapped_column(String(10), default=None)
    final_grade: Mapped[str | None] = mapped_column(String(10), default=None)
    inc_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    completion_status: Mapped[str | None] = mapped_column(String(30), default=None)
    remarks: Mapped[str | None] = mapped_column(String(100), default=None)
    is_draft: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    is_locked: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class GradeAssessment(UUIDMixin, Base):
    """Raw exam, quiz, and project component scores for a course section."""

    __tablename__ = "grade_assessments"

    grade_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student_grades.id", ondelete="CASCADE")
    )
    assessment_title: Mapped[str] = mapped_column(String(100))
    weight_percentage: Mapped[Decimal] = mapped_column(Numeric(precision=5, scale=2))
    score: Mapped[Decimal] = mapped_column(Numeric(precision=5, scale=2))
    max_score: Mapped[Decimal] = mapped_column(
        Numeric(precision=5, scale=2), default=Decimal("100.0"), server_default="100"
    )

    __table_args__ = (
        UniqueConstraint(
            "grade_id", "assessment_title", name="uq_grade_assessments_grade_title"
        ),
    )


class IncompleteCompletion(UUIDMixin, Base):
    """Fulfillment of incomplete (INC) grade within 1-year window."""

    __tablename__ = "incomplete_completions"

    grade_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student_grades.id", ondelete="CASCADE")
    )
    proposed_mark: Mapped[str] = mapped_column(String(10))
    completion_form_file_id: Mapped[str] = mapped_column(String(100))
    remarks: Mapped[str | None] = mapped_column(Text, default=None)
    state: Mapped[str] = mapped_column(
        String(30), default="PENDING_DEAN", server_default="PENDING_DEAN"
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    review_remarks: Mapped[str | None] = mapped_column(Text, default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("idx_fk_incomplete_completions_grade_id", "grade_id"),)


class GradeCorrection(UUIDMixin, Base):
    """Formal audit-tracked modification of an approved grade."""

    __tablename__ = "grade_corrections"

    grade_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student_grades.id", ondelete="CASCADE")
    )
    faculty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    current_mark: Mapped[str] = mapped_column(String(10))
    proposed_mark: Mapped[str] = mapped_column(String(10))
    reason: Mapped[str] = mapped_column(Text)
    grading_sheet_file_id: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(
        String(30), default="PENDING_CHAIR", server_default="PENDING_CHAIR"
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
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
    registrar_approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    registrar_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    rejection_remarks: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )

    __table_args__ = (Index("idx_fk_grade_corrections_grade_id", "grade_id"),)


class StudentTermGPA(UUIDMixin, Base):
    """Term-end GPA calculation and standing evaluation."""

    __tablename__ = "student_term_gpa"

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    academic_term_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_terms.id", ondelete="CASCADE")
    )
    total_gpa_units: Mapped[Decimal] = mapped_column(Numeric(precision=4, scale=1))
    term_gpa: Mapped[Decimal] = mapped_column(Numeric(precision=4, scale=2))
    cumulative_gpa: Mapped[Decimal] = mapped_column(Numeric(precision=4, scale=2))
    academic_standing: Mapped[str] = mapped_column(
        String(50), default="Good Standing", server_default="Good Standing"
    )
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "student_id",
            "academic_term_id",
            name="uq_student_term_gpa_student_term",
        ),
    )


class AcademicStandingAppeal(UUIDMixin, Base):
    """Student appeal against disqualification or warning standing."""

    __tablename__ = "academic_standing_appeals"

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    academic_term_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_terms.id", ondelete="CASCADE")
    )
    current_standing: Mapped[str] = mapped_column(String(50))
    appeal_reason: Mapped[str] = mapped_column(String(100))
    details: Mapped[str] = mapped_column(Text)
    supporting_doc_url: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(
        String(30), default="SUBMITTED", server_default="SUBMITTED"
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    review_notes: Mapped[str | None] = mapped_column(Text, default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("idx_fk_standing_appeals_student_id", "student_id"),)
