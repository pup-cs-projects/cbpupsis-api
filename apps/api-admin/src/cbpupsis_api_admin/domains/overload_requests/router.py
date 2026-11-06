from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.admin_auth.dependencies import (
    AdminSession,
    require_admin_session,
)
from cbpupsis_api_admin.domains.overload_requests import service
from cbpupsis_api_admin.domains.overload_requests.schemas import (
    OverloadDecisionRequest,
    OverloadRequestRead,
)
from cbpupsis_core.middleware import client_ip_or_none
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.enrollment.constants import (
    APPROVE_OVERLOAD_REQUEST,
    READ_OVERLOAD_REQUEST,
)
from cbpupsis_shared.domains.iam.dependencies import require_permission

router = APIRouter()


@router.get(
    "",
    response_model=list[OverloadRequestRead],
    operation_id="list_admin_overload_requests",
)
async def list_overload_requests(
    admin: AdminSession = Depends(require_admin_session),
    _authorized: CurrentUser = Depends(require_permission(READ_OVERLOAD_REQUEST)),
    db: AsyncSession = Depends(get_db),
) -> list[OverloadRequestRead]:
    return await service.list_requests(
        db,
        position=admin.position,
        department_id=admin.department_id,
        college_id=admin.college_id,
    )


@router.patch(
    "/{overload_request_id}",
    response_model=OverloadRequestRead,
    operation_id="decide_admin_overload_request",
    responses={
        404: {"description": "RESOURCE_NOT_FOUND when outside the Dean's college."},
        409: {"description": "REQUEST_ALREADY_DECIDED."},
        422: {"description": "DECISION_REMARKS_REQUIRED for a blank rejection."},
    },
)
async def decide_overload_request(
    overload_request_id: uuid.UUID,
    body: OverloadDecisionRequest,
    request: Request,
    admin: AdminSession = Depends(require_admin_session),
    _authorized: CurrentUser = Depends(require_permission(APPROVE_OVERLOAD_REQUEST)),
    db: AsyncSession = Depends(get_db),
) -> OverloadRequestRead:
    return await service.decide_request(
        db,
        overload_request_id=overload_request_id,
        decision=body.decision,
        remarks=body.remarks,
        reviewer_id=admin.id,
        position=admin.position,
        department_id=admin.department_id,
        college_id=admin.college_id,
        client_ip=client_ip_or_none(request),
    )
