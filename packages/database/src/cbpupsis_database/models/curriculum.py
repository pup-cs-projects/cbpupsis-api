"""SQLAlchemy models for academic programs, curricula, and courses domain."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Computed,
    ForeignKey,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from cbpupsis_database.base import Base, UUIDMixin


class Program(UUIDMixin, Base):
    """Degree program offering."""

    __tablename__ = "programs"

    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    department: Mapped[str] = mapped_column(String(100))
    total_units_required: Mapped[Decimal] = mapped_column(Numeric(precision=5, scale=1))
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )


class Curriculum(UUIDMixin, Base):
    """Versioned curriculum specification for a degree program."""

    __tablename__ = "curricula"

    program_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("programs.id", ondelete="CASCADE")
    )
    curriculum_year: Mapped[str] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )

    __table_args__ = (
        UniqueConstraint(
            "program_id", "curriculum_year", name="uq_curricula_prog_year"
        ),
        UniqueConstraint("id", "program_id", name="uq_curricula_id_prog"),
    )


class Course(UUIDMixin, Base):
    """Catalog course definition with lecture/lab unit breakdown."""

    __tablename__ = "courses"

    course_code: Mapped[str] = mapped_column(String(20), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, default=None)
    lecture_units: Mapped[Decimal] = mapped_column(
        Numeric(precision=3, scale=1), default=Decimal("0.0"), server_default="0"
    )
    lab_units: Mapped[Decimal] = mapped_column(
        Numeric(precision=3, scale=1), default=Decimal("0.0"), server_default="0"
    )
    total_units: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=3, scale=1),
        Computed("lecture_units + lab_units", persisted=True),
    )
    grading_mode: Mapped[str] = mapped_column(
        String(30), default="NUMERIC", server_default="NUMERIC"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )


class CurriculumCourse(UUIDMixin, Base):
    """Course mapped within a program curriculum at a target year/semester."""

    __tablename__ = "curriculum_courses"

    curriculum_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("curricula.id", ondelete="CASCADE")
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE")
    )
    year_level: Mapped[int] = mapped_column(SmallInteger)
    semester: Mapped[str] = mapped_column(String(30))
    course_type: Mapped[str] = mapped_column(
        String(30), default="core", server_default="core"
    )

    __table_args__ = (
        UniqueConstraint(
            "curriculum_id",
            "course_id",
            name="uq_curriculum_courses",
        ),
    )


class CoursePrerequisite(Base):
    """Rules specifying prerequisite/corequisite requirements for course
    registration."""

    __tablename__ = "course_prerequisites"

    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True
    )
    prerequisite_course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True
    )
    min_grade: Mapped[str | None] = mapped_column(
        String(10), default="3.00", server_default="3.00"
    )
    is_corequisite: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
