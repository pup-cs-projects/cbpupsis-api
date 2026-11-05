"""Waiver HTTP contracts; app composition supplies its live MFA admin guard."""

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.exceptions import ForbiddenError
from cbpupsis_core.pagination import Page, PaginationParams
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.enrollment import waiver
from cbpupsis_shared.domains.enrollment.constants import CREATE_WAIVER_REQUEST
from cbpupsis_shared.domains.enrollment.schemas import (
    WaiverDecision,
    WaiverFile,
    WaiverFiled,
    WaiverRead,
    WaiverReviewer,
)
from cbpupsis_shared.domains.iam.dependencies import require_permission

router = APIRouter()


async def filing_student(
    user: CurrentUser = Depends(require_permission(CREATE_WAIVER_REQUEST)),
):
    if user.role != "student":
        raise ForbiddenError("A student session is required.", code="INSUFFICIENT_ROLE")
    return user


@router.post(
    "/waiver",
    status_code=202,
    response_model=WaiverFiled,
    responses={
        409: {
            "description": "ENROLL_PREREQUISITE_NOT_MET or ENROLL_WAIVER_PENDING; "
            "eligibility is verified server-side."
        }
    },
)
async def file_waiver(
    request: Request,
    data: WaiverFile,
    user: CurrentUser = Depends(filing_student),
    db: AsyncSession = Depends(get_db),
):
    """File a session-owned unmet-edge waiver with CreateWaiverRequest.

    Requires a live student session. Conditions: REPEATED_PREREQUISITE
    (finalized 5.00 plus same-term retake, provisional policy), SUPERIOR_STANDING
    (canonical cumulative GWA <= 1.75), GRADUATING (stored applicable final term).
    No student identity or claims of eligibility are accepted in the body.
    """
    return await waiver.file_waiver(
        db,
        student_id=user.id,
        data=data,
        ip_address=request.client.host if request.client else None,
    )


def build_admin_router(read_guard, approve_guard):
    admin_router = APIRouter()

    @admin_router.get("", response_model=Page[WaiverRead])
    async def list_waivers(
        reviewer: WaiverReviewer = Depends(read_guard),
        pagination: PaginationParams = Depends(),
        db: AsyncSession = Depends(get_db),
    ):
        """Read MFA/permission-protected position-scoped waivers.

        Chair: own department; Dean: own explicitly mapped college;
        Registrar: institution-wide. Finance/other positions and students refused.
        """
        return await waiver.list_waivers(
            db, reviewer=reviewer, **pagination.model_dump()
        )

    @admin_router.patch(
        "/{waiverRequestId}",
        response_model=WaiverRead,
        responses={
            404: {
                "description": "RESOURCE_NOT_FOUND, including outside-scope targets."
            },
            409: {
                "description": "ENROLL_WAIVER_STEP_MISMATCH or REQUEST_ALREADY_DECIDED."
            },
            422: {
                "description": "DECISION_REMARKS_REQUIRED for rejection; "
                "VALIDATION_ERROR for final approval without vpaaReference."
            },
        },
    )
    async def decide_waiver(
        request: Request,
        waiverRequestId: uuid.UUID,
        data: WaiverDecision,
        reviewer: WaiverReviewer = Depends(approve_guard),
        db: AsyncSession = Depends(get_db),
    ):
        """Record one locked assigned-step decision, audit and durable student notice.

        Requires a live MFA-completed Admin session and ApproveWaiverRequest.
        Chair -> Dean -> Registrar; rejection is terminal at any pending step.
        Final approval records an external VPAA reference, not a credential.
        """
        return await waiver.decide_waiver(
            db,
            reviewer=reviewer,
            waiver_id=waiverRequestId,
            data=data,
            ip_address=request.client.host if request.client else None,
        )

    return admin_router
