"""Committed enrollment reads, independent of approval workflows."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.enrollment import repository
from cbpupsis_shared.domains.enrollment.schemas import EnrolledSection, RecordEnrollment


async def record_enrollments(db: AsyncSession, student_id: uuid.UUID):
    return [
        RecordEnrollment.model_validate(row, from_attributes=True)
        for row in await repository.list_record_enrollments(db, student_id)
    ]


async def enrolled_sections(
    db: AsyncSession, student_id: uuid.UUID, term_id: uuid.UUID
) -> list[EnrolledSection]:
    return [
        EnrolledSection.model_validate(row)
        for row in await repository.enrolled_sections(db, student_id, term_id)
    ]
