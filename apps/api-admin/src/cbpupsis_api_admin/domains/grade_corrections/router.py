"""One administrative endpoint family for Chair, Dean and Registrar."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.admin_auth.dependencies import (
    AdminSession,
    require_admin_permission,
)
from cbpupsis_api_admin.domains.grade_corrections import service
from cbpupsis_api_admin.domains.grade_corrections.schemas import (
    CorrectionDecision,
    CorrectionPage,
    CorrectionRead,
)
from cbpupsis_core.middleware import client_ip_or_none
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.grading.constants import APPROVE_GRADE_CORRECTION

_approver = require_admin_permission(APPROVE_GRADE_CORRECTION)


def private_correction_request(request: Request) -> None:
    request.state.private_response = True
    request.state.redact_validation_errors = True
    request.state.safe_error_metadata = True


router = APIRouter(
    dependencies=[Depends(private_correction_request)],
    responses={
        401: {"description": "AUTH_SESSION_EXPIRED or AUTH_MFA_REQUIRED."},
        403: {
            "description": "Missing permission, Finance denial or APPROVAL_SAME_ACTOR."
        },
        404: {
            "description": "RESOURCE_NOT_FOUND for missing or out-of-scope corrections."
        },
    },
)


def _scope(admin: AdminSession):
    return {
        "position": admin.position,
        "department_id": admin.department_id,
        "college_id": admin.college_id,
    }


@router.get(
    "", response_model=CorrectionPage, operation_id="list_admin_grade_corrections"
)
async def list_grade_corrections(
    response: Response,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    admin: AdminSession = Depends(_approver),
    db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = "private, no-store"
    return await service.list_corrections(
        db, **_scope(admin), limit=limit, offset=offset
    )


@router.get(
    "/{correction_id}",
    response_model=CorrectionRead,
    operation_id="get_admin_grade_correction",
)
async def get_grade_correction(
    correction_id: uuid.UUID,
    response: Response,
    admin: AdminSession = Depends(_approver),
    db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = "private, no-store"
    return await service.read_correction(db, correction_id, **_scope(admin))


@router.patch(
    "/{correction_id}",
    response_model=CorrectionRead,
    operation_id="decide_admin_grade_correction",
    responses={
        409: {
            "description": "APPROVAL_OUT_OF_ORDER or VERSION_CONFLICT; nothing changes."
        },
        422: {
            "description": "DECISION_REMARKS_REQUIRED when rejecting without remarks."
        },
    },
)
async def decide_grade_correction(
    correction_id: uuid.UUID,
    decision: CorrectionDecision,
    request: Request,
    response: Response,
    admin: AdminSession = Depends(_approver),
    db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = "private, no-store"
    return await service.decide_correction(
        db,
        correction_id,
        actor_id=admin.id,
        **_scope(admin),
        decision=decision,
        client_ip=client_ip_or_none(request),
    )
