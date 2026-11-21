from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.add_drop_requests import service
from cbpupsis_api_admin.domains.add_drop_requests.schemas import (
    AddDropDecisionRequest,
    AdminAddDropRequestRead,
)
from cbpupsis_api_admin.domains.admin_auth.dependencies import (
    AdminSession,
    require_admin_session,
)
from cbpupsis_core.middleware import client_ip_or_none
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.enrollment.constants import (
    APPROVE_ADD_DROP,
    READ_ADD_DROP_REQUEST,
)
from cbpupsis_shared.domains.iam.dependencies import require_permission

router = APIRouter()


@router.get(
    "",
    response_model=list[AdminAddDropRequestRead],
    operation_id="list_admin_add_drop_requests",
    responses={
        401: {"description": "An admin session is required."},
        403: {"description": "ReadAddDropRequest permission is required."},
    },
)
async def list_add_drop_requests(
    admin: AdminSession = Depends(require_admin_session),
    _authorized: CurrentUser = Depends(require_permission(READ_ADD_DROP_REQUEST)),
    db: AsyncSession = Depends(get_db),
) -> list[AdminAddDropRequestRead]:
    return await service.list_requests(
        db,
        position=admin.position,
        department_id=admin.department_id,
        college_id=admin.college_id,
    )


@router.patch(
    "/{request_id}",
    response_model=AdminAddDropRequestRead,
    operation_id="decide_admin_add_drop_request",
    responses={
        401: {"description": "An admin session is required."},
        403: {"description": "ApproveAddDrop permission is required."},
        404: {
            "description": (
                "RESOURCE_NOT_FOUND when outside the Chair's department or "
                "Dean's college."
            )
        },
        409: {
            "description": "REQUEST_ALREADY_DECIDED, "
            "REQUEST_CONCURRENCY_CONFLICT, the submit check codes for additions, or "
            "COR_ENROLLMENT_NOT_FINAL when the assessment or certificate cannot be "
            "updated. Nothing changes on a refusal."
        },
        422: {"description": "DECISION_REMARKS_REQUIRED for a blank rejection."},
    },
)
async def decide_add_drop_request(
    request_id: uuid.UUID,
    body: AddDropDecisionRequest,
    request: Request,
    admin: AdminSession = Depends(require_admin_session),
    _authorized: CurrentUser = Depends(require_permission(APPROVE_ADD_DROP)),
    db: AsyncSession = Depends(get_db),
) -> AdminAddDropRequestRead:
    return await service.decide_request(
        db,
        request_id=request_id,
        decision=body.decision,
        remarks=body.remarks,
        reviewer_id=admin.id,
        position=admin.position,
        department_id=admin.department_id,
        college_id=admin.college_id,
        client_ip=client_ip_or_none(request),
        version=body.version,
    )
