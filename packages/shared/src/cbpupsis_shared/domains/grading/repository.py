"""Data access for the grading domain.

Only this module builds SQL or calls into the session for grades and GPAs.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.grading import StudentTermGPA


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
