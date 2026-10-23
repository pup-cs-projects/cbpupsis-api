"""Read a student's own enrollment history for record composition."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.enrollment import repository
from cbpupsis_shared.domains.enrollment.schemas import RecordEnrollment


async def record_enrollments(db: AsyncSession, student_id: uuid.UUID):
    return [
        RecordEnrollment.model_validate(row, from_attributes=True)
        for row in await repository.list_record_enrollments(db, student_id)
    ]
