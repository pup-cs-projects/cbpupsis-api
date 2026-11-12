"""Admin waiver HTTP contracts; app composition supplies the live MFA guard."""

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.pagination import Page, PaginationParams
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.enrollment import waiver
from cbpupsis_shared.domains.enrollment.schemas import (
    WaiverDecision,
    WaiverRead,
    WaiverReviewer,
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
