import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.student_auth.dependencies import (
    require_student_session,
)
from cbpupsis_api_student.domains.student_enrollment import service
from cbpupsis_api_student.domains.student_enrollment.schemas import (
    AddDropRequestCreate,
    AddDropRequestRead,
    AvailableCourseRead,
    EnrollmentEligibilityRead,
    EnrollmentSubmitRead,
    EnrollmentSubmitRequest,
    OverloadRequestCreate,
    OverloadRequestFiledRead,
)
from cbpupsis_core.middleware import client_ip_or_none
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.enrollment.constants import (
    CREATE_ADD_DROP_REQUEST,
    CREATE_ENROLLMENT,
    CREATE_OVERLOAD_REQUEST,
    READ_ADD_DROP_REQUEST,
    READ_ENROLLMENT,
)
from cbpupsis_shared.domains.iam.dependencies import require_permission

router = APIRouter()


@router.post(
    "/overload",
    response_model=OverloadRequestFiledRead,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="create_student_overload_request",
    responses={
        409: {
            "description": "ENROLL_OVERLOAD_NOT_ELIGIBLE or "
            "ENROLL_OVERLOAD_CAP_EXCEEDED."
        }
    },
)
async def create_overload_request(
    body: OverloadRequestCreate,
    request: Request,
    user: CurrentUser = Depends(require_student_session),
    _authorized: CurrentUser = Depends(require_permission(CREATE_OVERLOAD_REQUEST)),
    db: AsyncSession = Depends(get_db),
) -> OverloadRequestFiledRead:
    return await service.file_overload_request(
        db,
        student_id=user.id,
        term_selector=body.term,
        requested_units=body.requested_units,
        justification=body.justification,
        client_ip=client_ip_or_none(request),
        role=user.role or "student",
    )


@router.post(
    "/submit",
    response_model=EnrollmentSubmitRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="submit_student_enrollment",
    responses={
        401: {"description": "A live student bearer session is required."},
        403: {"description": "CreateEnrollment permission is required."},
        404: {
            "description": "The term or a selected section is not available to "
            "the signed-in student's curriculum."
        },
        409: {
            "description": "The first failed enrollment check. Codes, in order: "
            "ENROLL_PERIOD_CLOSED, ENROLL_FINANCIAL_HOLD, ENROLL_HOLD_ACTIVE, "
            "ENROLL_STANDING_BLOCKED, the unit-ceiling codes, "
            "ENROLL_SCHEDULE_CONFLICT, prerequisite codes when enabled, and "
            "ENROLL_SECTION_FULL. No enrollment or seat is written."
        },
    },
)
async def submit_enrollment(
    body: EnrollmentSubmitRequest,
    request: Request,
    user: CurrentUser = Depends(require_student_session),
    _authorized: CurrentUser = Depends(require_permission(CREATE_ENROLLMENT)),
    db: AsyncSession = Depends(get_db),
) -> EnrollmentSubmitRead:
    return await service.submit_enrollment(
        db,
        student_id=user.id,
        term_selector=body.term,
        section_ids=body.section_ids,
        client_ip=client_ip_or_none(request),
        role=user.role or "student",
    )


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


@router.post(
    "/add-drop",
    response_model=AddDropRequestRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_student_add_drop_request",
    responses={
        401: {"description": "A live student bearer session is required."},
        403: {"description": "CreateAddDropRequest permission is required."},
        404: {"description": "Resource not found."},
        409: {
            "description": (
                "Filing refusal: closed period, below minimum load, "
                "section full, schedule conflict, unit ceiling exceeded, "
                "or prerequisite unmet."
            )
        },
    },
)
async def create_add_drop_request(
    body: AddDropRequestCreate,
    request: Request,
    user: CurrentUser = Depends(require_student_session),
    _authorized: CurrentUser = Depends(require_permission(CREATE_ADD_DROP_REQUEST)),
    db: AsyncSession = Depends(get_db),
) -> AddDropRequestRead:
    return await service.file_add_drop_request(
        db,
        student_id=user.id,
        request_data=body,
        client_ip=client_ip_or_none(request),
    )


@router.get(
    "/add-drop",
    response_model=list[AddDropRequestRead],
    operation_id="list_student_add_drop_requests",
    responses={
        401: {"description": "A live student bearer session is required."},
        403: {"description": "ReadAddDropRequest permission is required."},
    },
)
async def list_add_drop_requests(
    user: CurrentUser = Depends(require_student_session),
    _authorized: CurrentUser = Depends(require_permission(READ_ADD_DROP_REQUEST)),
    db: AsyncSession = Depends(get_db),
) -> list[AddDropRequestRead]:
    return await service.list_student_add_drop_requests(db, student_id=user.id)


@router.get(
    "/add-drop/{request_id}",
    response_model=AddDropRequestRead,
    operation_id="get_student_add_drop_request",
    responses={
        401: {"description": "A live student bearer session is required."},
        403: {"description": "ReadAddDropRequest permission is required."},
        404: {"description": "Resource not found."},
    },
)
async def get_add_drop_request(
    request_id: uuid.UUID,
    user: CurrentUser = Depends(require_student_session),
    _authorized: CurrentUser = Depends(require_permission(READ_ADD_DROP_REQUEST)),
    db: AsyncSession = Depends(get_db),
) -> AddDropRequestRead:
    return await service.get_student_add_drop_request(
        db, student_id=user.id, request_id=request_id
    )
