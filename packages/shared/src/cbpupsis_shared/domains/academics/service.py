"""Academic calendar rules."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.academics import repository
from cbpupsis_shared.domains.academics.schemas import HolidayRead, TermRead


async def resolve_term(
    db: AsyncSession, year: str | None, semester: str | None
) -> TermRead | None:
    rows = await repository.find_terms(db, year, semester)
    return TermRead.model_validate(rows[0]) if len(rows) == 1 else None


async def holidays(db: AsyncSession, term_id: uuid.UUID) -> list[HolidayRead]:
    return [
        HolidayRead.model_validate(row)
        for row in await repository.holidays(db, term_id)
    ]
