"""Committed enrollment reads, independent of approval workflows."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.enrollment import repository
from cbpupsis_shared.domains.enrollment.schemas import EnrolledSection


async def enrolled_sections(
    db: AsyncSession, student_id: uuid.UUID, term_id: uuid.UUID
) -> list[EnrolledSection]:
    return [
        EnrolledSection.model_validate(row)
        for row in await repository.enrolled_sections(db, student_id, term_id)
    ]
