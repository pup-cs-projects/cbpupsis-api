"""Internal client for the grading domain: the extraction swap point."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.grading import service
from cbpupsis_shared.domains.grading.schemas import (
    ScholasticStandingRead,
    SectionRosterGrade,
)
from cbpupsis_shared.domains.grading.service import academic_record as academic_record
from cbpupsis_shared.domains.grading.service import read_grades, read_transcript

__all__ = [
    "ScholasticStandingRead",
    "SectionRosterGrade",
    "get_previous_regular_term_gwa",
    "get_student_scholastic_standing",
    "read_grades",
    "read_transcript",
    "section_roster_grades",
]


async def section_roster_grades(
    db: AsyncSession, enrollment_ids: set[uuid.UUID]
) -> dict[uuid.UUID, SectionRosterGrade]:
    return await service.section_roster_grades(db, enrollment_ids)


async def get_previous_regular_term_gwa(
    db: AsyncSession, *, student_id: uuid.UUID, before: date
) -> Decimal | None:
    return await service.get_previous_regular_term_gwa(
        db, student_id=student_id, before=before
    )


async def get_student_scholastic_standing(
    db: AsyncSession, student_id: uuid.UUID
) -> ScholasticStandingRead:
    return await service.get_student_scholastic_standing(db, student_id)
