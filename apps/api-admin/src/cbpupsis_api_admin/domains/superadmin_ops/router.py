"""Superadmin-only operations; mounted beneath /api/v1/superadmin.

Two checks on every endpoint: the permission (``require_permission``, which
also requires a Superadmin session because these actions are Superadmin-only)
and the live Superadmin actor check in the service.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.superadmin_ops import service
from cbpupsis_api_admin.domains.superadmin_ops.constants import (
    APPROVE_BACKUP_RESTORE,
    MANAGE_OVERRIDE,
    REQUEST_BACKUP_RESTORE,
)
from cbpupsis_api_admin.domains.superadmin_ops.schemas import (
    OverrideRequest,
    OverrideResult,
    RestoreRequestRead,
)
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.iam.dependencies import require_permission

router = APIRouter()


@router.post("/overrides/{rule_id}", response_model=OverrideResult)
async def override(
    rule_id: str,
    data: OverrideRequest,
    actor: CurrentUser = Depends(require_permission(MANAGE_OVERRIDE)),
    db: AsyncSession = Depends(get_db),
) -> OverrideResult:
    return await service.execute_override(
        db, actor_id=actor.id, rule_id=rule_id, data=data
    )


@router.post(
    "/backups/{backup_id}/restore-requests",
    response_model=RestoreRequestRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_restore(
    backup_id: uuid.UUID,
    actor: CurrentUser = Depends(require_permission(REQUEST_BACKUP_RESTORE)),
    db: AsyncSession = Depends(get_db),
) -> RestoreRequestRead:
    return await service.request_restore(db, actor_id=actor.id, backup_id=backup_id)


@router.post(
    "/backups/{backup_id}/restore-requests/approve", response_model=RestoreRequestRead
)
async def approve_restore(
    backup_id: uuid.UUID,
    actor: CurrentUser = Depends(require_permission(APPROVE_BACKUP_RESTORE)),
    db: AsyncSession = Depends(get_db),
) -> RestoreRequestRead:
    return await service.approve_restore(db, actor_id=actor.id, backup_id=backup_id)
