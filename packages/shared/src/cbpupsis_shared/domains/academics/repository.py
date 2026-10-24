"""SQL for calendar and term metadata reads."""

import uuid
from collections.abc import Collection

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.academics import AcademicEvent, AcademicTerm


async def list_terms(
    db: AsyncSession, ids: Collection[uuid.UUID]
) -> list[AcademicTerm]:
    """Read only terms referenced by the caller's record.

    SQL::

        SELECT * FROM academic_terms WHERE id IN (:ids);
    """
    if not ids:
        return []
    return list(await db.scalars(select(AcademicTerm).where(AcademicTerm.id.in_(ids))))


async def find_terms(db: AsyncSession, year: str | None, semester: str | None):
    """Find a selected term or the current term; ambiguity is not hidden.

    SQL::

        SELECT * FROM academic_terms
        WHERE academic_year = :year AND semester = :semester LIMIT 2;
        SELECT * FROM academic_terms WHERE is_current = true LIMIT 2;
    """
    query = select(AcademicTerm)
    if year is None:
        query = query.where(AcademicTerm.is_current.is_(True))
    else:
        query = query.where(
            AcademicTerm.academic_year == year, AcademicTerm.semester == semester
        )
    return list(await db.scalars(query.limit(2)))


async def holidays(db: AsyncSession, term_id: uuid.UUID):
    """Read configured holidays, including inclusive multi-day closures.

    SQL::

        SELECT * FROM academic_events
        WHERE academic_term_id = :term_id AND lower(event_type) = 'holiday'
        LIMIT 1001;
    """
    return list(
        await db.scalars(
            select(AcademicEvent)
            .where(
                AcademicEvent.academic_term_id == term_id,
                func.lower(AcademicEvent.event_type) == "holiday",
            )
            .limit(1001)
        )
    )
