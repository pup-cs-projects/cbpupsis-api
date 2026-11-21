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
from cbpupsis_shared.domains.enrollment import client as enrollment
from cbpupsis_shared.domains.enrollment.constants import (
    CREATE_ADD_DROP_REQUEST,
    CREATE_ENROLLMENT,
    CREATE_OVERLOAD_REQUEST,
    CREATE_WAIVER_REQUEST,
    READ_ADD_DROP_REQUEST,
    READ_ENROLLMENT,
)
from cbpupsis_shared.domains.enrollment.schemas import (
    WaiverFile,
    WaiverFiled,
    WaiverRead,
)
from cbpupsis_shared.domains.iam.dependencies import require_permission

router = APIRouter()


async def filing_student(
    user: CurrentUser = Depends(require_student_session),
    _authorized: CurrentUser = Depends(require_permission(CREATE_WAIVER_REQUEST)),
) -> CurrentUser:
    return user


@router.post(
    "/waiver",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WaiverFiled,
    responses={
        409: {
            "description": "ENROLL_PREREQUISITE_NOT_MET when the student meets "
            "none of the waiver conditions, or ENROLL_WAIVER_PENDING when a live "
            "waiver already exists for the section. Eligibility is verified "
            "server-side."
        }
    },
)
async def file_waiver(
    request: Request,
    data: WaiverFile,
    user: CurrentUser = Depends(filing_student),
    db: AsyncSession = Depends(get_db),
) -> WaiverFiled:
    """File a session-owned unmet-edge waiver with CreateWaiverRequest."""
    return await enrollment.file_waiver(
        db, student_id=user.id, data=data, ip_address=client_ip_or_none(request)
    )


@router.get(
    "/waiver",
    response_model=list[WaiverRead],
    operation_id="list_student_waiver_requests",
    responses={
        401: {"description": "A live student bearer session is required."},
        403: {"description": "ReadEnrollment permission is required."},
    },
)
async def list_own_waivers(
    user: CurrentUser = Depends(require_student_session),
    _authorized: CurrentUser = Depends(require_permission(READ_ENROLLMENT)),
    db: AsyncSession = Depends(get_db),
) -> list[WaiverRead]:
    """List the signed-in student's own waivers, newest first.

    `state` is PENDING_CHAIR, PENDING_DEAN, PENDING_VPAA, APPROVED or REJECTED;
    `reviewRemarks` carries the reviewer's remarks.
    """
    return await enrollment.list_student_waivers(db, student_id=user.id)


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
        academic_term_id=body.academic_term_id,
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
            "ENROLL_STANDING_BLOCKED, ENROLL_UNIT_CEILING_EXCEEDED, "
            "ENROLL_OVERLOAD_CAP_EXCEEDED, ENROLL_SUMMER_SUBJECT_LIMIT, "
            "ENROLL_SCHEDULE_CONFLICT, ENROLL_PREREQUISITE_NOT_MET, "
            "ENROLL_THIRD_YEAR_GATE, ENROLL_PE_SEQUENCE, ENROLL_WAIVER_PENDING, "
            "ENROLL_ACADEMIC_DATA_UNAVAILABLE, ENROLL_DUPLICATE_COURSE, "
            "ENROLL_ASSESSMENT_UNAVAILABLE and ENROLL_SECTION_FULL. "
            "No enrollment or seat is written."
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
        academic_term_id=body.academic_term_id,
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
    academic_term_id: uuid.UUID = Query(
        ...,
        alias="academicTermId",
        description="Academic-term UUID, as returned by eligibility.",
    ),
    user: CurrentUser = Depends(require_student_session),
    _authorized: CurrentUser = Depends(require_permission(READ_ENROLLMENT)),
    db: AsyncSession = Depends(get_db),
) -> list[AvailableCourseRead]:
    return await service.get_available_courses(
        db, student_id=user.id, academic_term_id=academic_term_id
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
                "Filing refusal; no request is created. "
                "ENROLL_ADD_DROP_PERIOD_CLOSED (details periodStart, periodEnd), "
                "ENROLL_BELOW_MINIMUM_LOAD (details minimumLoad, unitsAfter), "
                "REQUEST_ALREADY_PENDING, or any submit check on an addition: "
                "ENROLL_FINANCIAL_HOLD, ENROLL_HOLD_ACTIVE, "
                "ENROLL_STANDING_BLOCKED, ENROLL_UNIT_CEILING_EXCEEDED, "
                "ENROLL_OVERLOAD_CAP_EXCEEDED, ENROLL_SUMMER_SUBJECT_LIMIT, "
                "ENROLL_SCHEDULE_CONFLICT, ENROLL_PREREQUISITE_NOT_MET, "
                "ENROLL_THIRD_YEAR_GATE, ENROLL_PE_SEQUENCE, "
                "ENROLL_WAIVER_PENDING, ENROLL_DUPLICATE_COURSE, "
                "ENROLL_SECTION_FULL."
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
