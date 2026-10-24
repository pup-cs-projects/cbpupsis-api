"""Grading-owned SQL; callers supply owner-scoped enrollment identifiers."""

import uuid
from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.grading import StudentGrade, StudentTermGPA


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
