"""Internal client for the academics domain: the extraction swap point."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.academics import service
from cbpupsis_shared.domains.academics.schemas import (
    AcademicEventRead,
    AcademicPeriodRead,
    AcademicTermRead,
)
from cbpupsis_shared.domains.academics.service import (
    holidays,
    record_terms,
    resolve_term,
)

__all__ = [
    "AcademicEventRead",
    "AcademicPeriodRead",
    "AcademicTermRead",
    "get_current_term",
    "get_term",
    "get_term_periods",
    "holidays",
    "list_upcoming_events",
    "record_terms",
    "resolve_term",
]


async def get_current_term(db: AsyncSession) -> AcademicTermRead | None:
    return await service.get_current_term(db)


async def get_term(
    db: AsyncSession, academic_year: str, semester: str
) -> AcademicTermRead | None:
    return await service.get_term(db, academic_year, semester)


def get_term_periods(
    term: AcademicTermRead, as_of: datetime | None = None
) -> list[AcademicPeriodRead]:
    return service.get_term_periods(term, as_of)


async def list_upcoming_events(
    db: AsyncSession, term_id: uuid.UUID, limit: int = 10
) -> list[AcademicEventRead]:
    return await service.list_upcoming_events(db, term_id, limit=limit)
