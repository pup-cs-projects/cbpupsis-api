from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.student_auth.dependencies import (
    require_student_session,
)
from cbpupsis_api_student.domains.student_enrollment import service
from cbpupsis_api_student.domains.student_enrollment.schemas import (
    EnrollmentEligibilityRead,
)
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.enrollment.constants import READ_ENROLLMENT
from cbpupsis_shared.domains.iam.dependencies import require_permission

router = APIRouter()


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
