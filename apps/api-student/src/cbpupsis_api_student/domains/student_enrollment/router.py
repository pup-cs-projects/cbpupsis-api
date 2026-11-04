from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.student_auth.dependencies import (
    require_student_session,
)
from cbpupsis_api_student.domains.student_enrollment import service
from cbpupsis_api_student.domains.student_enrollment.schemas import (
    AvailableCourseRead,
    EnrollmentEligibilityRead,
)
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.enrollment.constants import READ_ENROLLMENT
from cbpupsis_shared.domains.iam.dependencies import require_permission

router = APIRouter()


@router.get(
    "/available-courses",
    response_model=list[AvailableCourseRead],
    operation_id="list_student_enrollment_available_courses",
)
async def get_available_courses(
    term: str = Query(
        ...,
        min_length=3,
        description=(
            "Stored academic year and semester joined by a hyphen, for example "
            "2026-2027-First Semester."
        ),
    ),
    user: CurrentUser = Depends(require_student_session),
    _authorized: CurrentUser = Depends(require_permission(READ_ENROLLMENT)),
    db: AsyncSession = Depends(get_db),
) -> list[AvailableCourseRead]:
    return await service.get_available_courses(
        db, student_id=user.id, term_selector=term
    )


@router.get(
    "/eligibility",
    response_model=EnrollmentEligibilityRead,
    operation_id="get_student_enrollment_eligibility",
)
async def get_enrollment_eligibility(
    user: CurrentUser = Depends(require_student_session),
    _authorized: CurrentUser = Depends(require_permission(READ_ENROLLMENT)),
    db: AsyncSession = Depends(get_db),
) -> EnrollmentEligibilityRead:
    return await service.get_eligibility(db, student_id=user.id)
