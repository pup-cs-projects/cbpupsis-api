"""Grading-owned SQL; callers supply owner-scoped enrollment identifiers."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.grading import (
    GradeCorrection,
    SectionGradeSubmission,
    StudentGrade,
    StudentTermGPA,
)


def stage_correction(db: AsyncSession, **values):
    """Stage one correction and its private document in the owner transaction.

    SQL::

        INSERT INTO grade_corrections
            (id, grade_id, faculty_id, section_id, student_id, current_mark,
             proposed_mark, reason, grading_sheet_file_id, grading_sheet_content,
             grading_sheet_content_type, grading_sheet_size, grading_sheet_sha256,
             window_closed_at, grade_updated_at, state, version, created_at)
        VALUES (:id, :grade_id, :faculty_id, :section_id, :student_id, :current_mark,
                :proposed_mark, :reason, :file_id, :content, :content_type, :size,
                :sha256, :window_closed_at, :grade_updated_at, :state, :version,
                :created_at);
    """
    row = GradeCorrection(**values)
    db.add(row)
    return row


async def correction(db: AsyncSession, correction_id, *, lock=False):
    """Read fresh state, optionally serializing competing decisions.

    SQL::

        SELECT * FROM grade_corrections WHERE id = :id FOR UPDATE;
    """
    query = select(GradeCorrection).where(GradeCorrection.id == correction_id)
    if lock:
        query = query.with_for_update()
    return await db.scalar(query.execution_options(populate_existing=True))


async def correction_page(
    db: AsyncSession, *, faculty_id=None, section_ids=None, limit=50, offset=0
):
    """Apply visibility predicates before pagination, never after a global page.

    SQL::

        SELECT * FROM grade_corrections
        WHERE faculty_id = :faculty_id AND section_id IN (:section_ids)
        ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset;
        SELECT count(*) FROM grade_corrections
        WHERE faculty_id = :faculty_id AND section_id IN (:section_ids);
    """
    predicates = []
    if faculty_id is not None:
        predicates.append(GradeCorrection.faculty_id == faculty_id)
    if section_ids is not None:
        predicates.append(GradeCorrection.section_id.in_(section_ids))
    total = await db.scalar(
        select(func.count()).select_from(GradeCorrection).where(*predicates)
    )
    rows = await db.scalars(
        select(GradeCorrection)
        .where(*predicates)
        .order_by(GradeCorrection.created_at.desc(), GradeCorrection.id.desc())
        .limit(limit)
        .offset(offset)
        .execution_options(populate_existing=True)
    )
    return list(rows), total


async def lock_grade(db: AsyncSession, *, grade_id=None, enrollment_id=None):
    """Coordinate corrections using the same grade-row lock as finalization.

    SQL::

        SELECT * FROM student_grades WHERE id = :grade_id FOR UPDATE;
    """
    query = select(StudentGrade)
    if grade_id is not None:
        query = query.where(StudentGrade.id == grade_id)
    else:
        query = query.where(StudentGrade.enrollment_item_id == enrollment_id)
    return await db.scalar(
        query.with_for_update().execution_options(populate_existing=True)
    )


async def lock_student_gpas(db: AsyncSession, student_id):
    """Read existing evaluations without inventing academic-standing decisions.

    SQL::

        SELECT * FROM student_term_gpa WHERE student_id = :student_id
        ORDER BY academic_term_id FOR UPDATE;
    """
    return list(
        await db.scalars(
            select(StudentTermGPA)
            .where(StudentTermGPA.student_id == student_id)
            .order_by(StudentTermGPA.academic_term_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


async def get_latest_student_term_gpa(
    db: AsyncSession, student_id: uuid.UUID
) -> StudentTermGPA | None:
    """Return the most recent term GPA calculation and standing for a student.

    SQL::

        SELECT student_term_gpa.id, student_term_gpa.student_id,
               student_term_gpa.academic_term_id, student_term_gpa.total_gpa_units,
               student_term_gpa.term_gpa, student_term_gpa.cumulative_gpa,
               student_term_gpa.academic_standing, student_term_gpa.calculated_at
        FROM student_term_gpa
        WHERE student_term_gpa.student_id = :student_id
        ORDER BY student_term_gpa.calculated_at DESC
        LIMIT 1
    """
    stmt = (
        select(StudentTermGPA)
        .where(StudentTermGPA.student_id == student_id)
        .order_by(StudentTermGPA.calculated_at.desc())
        .limit(1)
    )
    return await db.scalar(stmt)


async def list_student_term_gpas(db: AsyncSession, *, student_id: uuid.UUID):
    """Read stored GPA evaluations for service-side term selection.

    SQL::

        SELECT * FROM student_term_gpa
        WHERE student_id = :student_id
        ORDER BY calculated_at DESC;
    """
    return list(
        await db.scalars(
            select(StudentTermGPA)
            .where(StudentTermGPA.student_id == student_id)
            .order_by(StudentTermGPA.calculated_at.desc())
        )
    )


async def posted_enrollment_ids(db: AsyncSession, ids: Collection[uuid.UUID]):
    """Find which owner-scoped enrollments have published final marks.

    SQL::

        SELECT enrollment_item_id FROM student_grades
        WHERE enrollment_item_id IN (:ids) AND NOT is_draft AND is_locked
          AND final_grade IS NOT NULL AND submitted_at IS NOT NULL;
    """
    if not ids:
        return set()
    return set(
        await db.scalars(
            select(StudentGrade.enrollment_item_id).where(
                StudentGrade.enrollment_item_id.in_(ids),
                StudentGrade.is_draft.is_(False),
                StudentGrade.is_locked.is_(True),
                StudentGrade.final_grade.is_not(None),
                StudentGrade.submitted_at.is_not(None),
            )
        )
    )


async def published_marks(db: AsyncSession, ids: Collection[uuid.UUID]):
    """Read fresh posted marks, even if an earlier read populated this session.

    SQL::

        SELECT * FROM student_grades WHERE enrollment_item_id IN (:ids)
          AND NOT is_draft AND is_locked
          AND final_grade IS NOT NULL AND submitted_at IS NOT NULL;
    """
    if not ids:
        return []
    return list(
        await db.scalars(
            select(StudentGrade)
            .where(
                StudentGrade.enrollment_item_id.in_(ids),
                StudentGrade.is_draft.is_(False),
                StudentGrade.is_locked.is_(True),
                StudentGrade.final_grade.is_not(None),
                StudentGrade.submitted_at.is_not(None),
            )
            .execution_options(populate_existing=True)
        )
    )


async def roster_grades(db: AsyncSession, ids: Collection[uuid.UUID]):
    """Read grade workflow state for owner-scoped course enrollments.

    SQL::

        SELECT * FROM student_grades WHERE enrollment_item_id IN (:ids);
    """
    if not ids:
        return []
    return list(
        await db.scalars(
            select(StudentGrade).where(StudentGrade.enrollment_item_id.in_(ids))
        )
    )


async def evaluated_standing(
    db: AsyncSession, student_id: uuid.UUID, term_id: uuid.UUID
):
    """Read the standing evaluated by #105, never substitute its stored averages.

    SQL::

        SELECT * FROM student_term_gpa
        WHERE student_id = :student_id AND academic_term_id = :term_id;
    """
    return await db.scalar(
        select(StudentTermGPA)
        .where(
            StudentTermGPA.student_id == student_id,
            StudentTermGPA.academic_term_id == term_id,
        )
        .execution_options(populate_existing=True)
    )


async def get_section_submission(
    db: AsyncSession, section_id: uuid.UUID, *, lock: bool = False
) -> SectionGradeSubmission | None:
    """Return the section grade submission record, optionally locked for update.

    SQL::

        SELECT * FROM section_grade_submissions
        WHERE section_id = :section_id [FOR UPDATE];
    """
    stmt = (
        select(SectionGradeSubmission)
        .where(SectionGradeSubmission.section_id == section_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        stmt = stmt.with_for_update()
    return await db.scalar(stmt)


def add_section_submission(
    db: AsyncSession,
    *,
    section_id: uuid.UUID,
    state: str,
    version: int,
    updated_at: datetime,
) -> SectionGradeSubmission:
    """Stage a new section grade submission header.

    On the caller's flush or commit::

        INSERT INTO section_grade_submissions
            (id, section_id, state, version, finalized_by, finalized_at, updated_at)
        VALUES (:id, :section_id, :state, :version, NULL, NULL, :updated_at);
    """
    row = SectionGradeSubmission(
        section_id=section_id,
        state=state,
        version=version,
        updated_at=updated_at,
    )
    db.add(row)
    return row


async def roster_grades_for_update(
    db: AsyncSession, ids: Collection[uuid.UUID]
) -> list[StudentGrade]:
    """Lock and return grade rows for the given course enrollment identifiers.

    SQL::

        SELECT * FROM student_grades
        WHERE enrollment_item_id IN (:ids)
        ORDER BY id
        FOR UPDATE;
    """
    if not ids:
        return []
    return list(
        await db.scalars(
            select(StudentGrade)
            .where(StudentGrade.enrollment_item_id.in_(ids))
            .order_by(StudentGrade.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


def add_student_grade(
    db: AsyncSession,
    *,
    enrollment_item_id: uuid.UUID,
    raw_score: Decimal | None,
    midterm_grade: str | None,
    final_grade: str | None,
    remarks: str | None,
    is_draft: bool,
    is_locked: bool,
    updated_at: datetime,
) -> StudentGrade:
    """Stage a new StudentGrade row on the caller's session.

    On the caller's flush or commit::

        INSERT INTO student_grades
            (id, enrollment_item_id, raw_score, midterm_grade, final_grade,
             remarks, is_draft, is_locked, updated_at)
        VALUES (:id, :enrollment_item_id, :raw_score, :midterm_grade,
                :final_grade, :remarks, :is_draft, :is_locked, :updated_at);
    """
    row = StudentGrade(
        enrollment_item_id=enrollment_item_id,
        raw_score=raw_score,
        midterm_grade=midterm_grade,
        final_grade=final_grade,
        remarks=remarks,
        is_draft=is_draft,
        is_locked=is_locked,
        updated_at=updated_at,
    )
    db.add(row)
    return row
