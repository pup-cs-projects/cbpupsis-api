"""Internal client for the enrollment domain: the extraction swap point."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.enrollment import service
from cbpupsis_shared.domains.enrollment.schemas import (
    EnrollmentSummaryRead,
    TermEnrollmentRead,
)
from cbpupsis_shared.domains.enrollment.service import (
    enrolled_sections,
    record_enrollments,
)

__all__ = [
    "EnrollmentSummaryRead",
    "TermEnrollmentRead",
    "enrolled_sections",
    "evaluate_academics",
    "get_enrollment_summary",
    "get_term_enrollment",
    "record_enrollments",
    "require_academics",
]


async def evaluate_academics(db: AsyncSession, *, student_id, term, section_ids):
    # Grading reads enrollment history; defer the orchestration import so the
    # two domain clients do not create an import-time cycle.
    from cbpupsis_shared.domains.enrollment.academic import (  # noqa: PLC0415
        evaluate_academics as evaluate,
    )

    return await evaluate(db, student_id=student_id, term=term, section_ids=section_ids)


def require_academics(verdict):
    from cbpupsis_shared.domains.enrollment.academic import (  # noqa: PLC0415
        require_academics as require,
    )

    return require(verdict)


async def get_term_enrollment(
    db: AsyncSession, student_id: uuid.UUID, academic_term_id: uuid.UUID
) -> TermEnrollmentRead | None:
    return await service.get_term_enrollment(db, student_id, academic_term_id)


async def get_enrollment_summary(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    academic_term_id: uuid.UUID,
    prescribed_load: Decimal,
    standing_reduction: Decimal,
) -> EnrollmentSummaryRead:
    return await service.get_enrollment_summary(
        db,
        student_id=student_id,
        academic_term_id=academic_term_id,
        prescribed_load=prescribed_load,
        standing_reduction=standing_reduction,
    )
