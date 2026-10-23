"""Data access for the academics domain.

Only this module builds SQL or calls into the session for academic terms and events.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.academics import AcademicEvent, AcademicTerm


async def get_current_term(db: AsyncSession) -> AcademicTerm | None:
    """Return the active academic term marked current, or the newest term.

    SQL::

        SELECT academic_terms.id, academic_terms.academic_year,
               academic_terms.semester, academic_terms.is_current,
               academic_terms.start_date, academic_terms.end_date,
               academic_terms.early_enrollment_start,
               academic_terms.early_enrollment_end,
               academic_terms.regular_enrollment_start,
               academic_terms.regular_enrollment_end,
               academic_terms.late_enrollment_end,
               academic_terms.add_drop_deadline,
               academic_terms.grade_submission_deadline
        FROM academic_terms
        WHERE academic_terms.is_current IS true
        LIMIT 1
    """
    stmt = (
        select(AcademicTerm)
        .where(AcademicTerm.is_current.is_(True))
        .order_by(AcademicTerm.start_date.desc())
        .limit(1)
    )
    term = await db.scalar(stmt)
    if term is None:
        fallback_stmt = (
            select(AcademicTerm).order_by(AcademicTerm.start_date.desc()).limit(1)
        )
        term = await db.scalar(fallback_stmt)
    return term


async def get_term_by_year_and_semester(
    db: AsyncSession, academic_year: str, semester: str
) -> AcademicTerm | None:
    """Return the academic term matching the year and semester.

    SQL::

        SELECT academic_terms.id, academic_terms.academic_year,
               academic_terms.semester, academic_terms.is_current,
               academic_terms.start_date, academic_terms.end_date
        FROM academic_terms
        WHERE academic_terms.academic_year = :academic_year
          AND academic_terms.semester = :semester
        LIMIT 1
    """
    stmt = (
        select(AcademicTerm)
        .where(
            AcademicTerm.academic_year == academic_year,
            AcademicTerm.semester == semester,
        )
        .limit(1)
    )
    return await db.scalar(stmt)


async def list_events_for_term(
    db: AsyncSession, term_id: uuid.UUID, limit: int = 10
) -> list[AcademicEvent]:
    """Return upcoming academic events for a term.

    SQL::

        SELECT academic_events.id, academic_events.academic_term_id,
               academic_events.title, academic_events.description,
               academic_events.start_date, academic_events.end_date,
               academic_events.event_type
        FROM academic_events
        WHERE academic_events.academic_term_id = :term_id
        ORDER BY academic_events.start_date ASC
        LIMIT :limit
    """
    stmt = (
        select(AcademicEvent)
        .where(AcademicEvent.academic_term_id == term_id)
        .order_by(AcademicEvent.start_date.asc())
        .limit(limit)
    )
    result = await db.scalars(stmt)
    return list(result)
