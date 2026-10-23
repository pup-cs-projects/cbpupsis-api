"""Internal client for the grading domain: the extraction swap point."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.grading import service
from cbpupsis_shared.domains.grading.schemas import ScholasticStandingRead

__all__ = [
    "ScholasticStandingRead",
    "get_student_scholastic_standing",
]


async def get_student_scholastic_standing(
    db: AsyncSession, student_id: uuid.UUID
) -> ScholasticStandingRead:
    return await service.get_student_scholastic_standing(db, student_id)
