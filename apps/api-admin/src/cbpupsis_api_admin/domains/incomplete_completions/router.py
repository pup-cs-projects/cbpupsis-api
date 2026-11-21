"""HTTP routing for College Dean review of Incomplete completions."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.admin_auth.dependencies import (
    AdminSession,
    require_admin_permission,
)
from cbpupsis_api_admin.domains.incomplete_completions import service
from cbpupsis_api_admin.domains.incomplete_completions.schemas import (
    IncompleteCompletionDecisionRequest,
    IncompleteCompletionRead,
)
from cbpupsis_core.middleware import client_ip_or_none
from cbpupsis_core.pagination import Page
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.grading.constants import APPROVE_INCOMPLETE_COMPLETION

router = APIRouter()


@router.get(
    "",
    response_model=Page[IncompleteCompletionRead],
    operation_id="list_admin_incomplete_completions",
    responses={
        401: {
            "description": "AUTH_SESSION_EXPIRED or AUTH_MFA_REQUIRED when the "
            "Admin session is missing or unverified."
        },
        403: {
            "description": "ApproveIncompleteCompletion permission is required "
            "(College Dean only)."
        },
    },
)
async def list_incomplete_completions(
    state: Literal["PENDING_DEAN", "ENDORSED", "REJECTED", "EXPIRED"] | None = Query(
        default=None
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    admin: AdminSession = Depends(
        require_admin_permission(APPROVE_INCOMPLETE_COMPLETION)
    ),
    db: AsyncSession = Depends(get_db),
) -> Page[IncompleteCompletionRead]:
    """List incomplete completion filings within the Dean's own college."""
    return await service.list_admin_incomplete_completions(
        db,
        position=admin.position,
        college_id=admin.college_id,
        state=state,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/{completion_id}",
    response_model=IncompleteCompletionRead,
    operation_id="decide_admin_incomplete_completion",
    responses={
        401: {
            "description": "AUTH_SESSION_EXPIRED or AUTH_MFA_REQUIRED when the "
            "Admin session is missing or unverified."
        },
        403: {
            "description": "ApproveIncompleteCompletion permission is required "
            "(College Dean only)."
        },
        404: {
            "description": "RESOURCE_NOT_FOUND when the completion does not exist "
            "or belongs to another college."
        },
        409: {
            "description": "INC_COMPLETION_WINDOW_CLOSED if already expired to "
            "5.00, REQUEST_ALREADY_DECIDED, or VERSION_CONFLICT."
        },
        422: {
            "description": "DECISION_REMARKS_REQUIRED when rejecting without remarks."
        },
    },
)
async def decide_incomplete_completion(
    completion_id: uuid.UUID,
    body: IncompleteCompletionDecisionRequest,
    request: Request,
    admin: AdminSession = Depends(
        require_admin_permission(APPROVE_INCOMPLETE_COMPLETION)
    ),
    db: AsyncSession = Depends(get_db),
) -> IncompleteCompletionRead:
    """Endorse or reject an Incomplete completion within the Dean's own college."""
    return await service.decide_incomplete_completion(
        db,
        completion_id=completion_id,
        reviewer_id=admin.id,
        position=admin.position,
        college_id=admin.college_id,
        payload=body,
        client_ip=client_ip_or_none(request),
    )
