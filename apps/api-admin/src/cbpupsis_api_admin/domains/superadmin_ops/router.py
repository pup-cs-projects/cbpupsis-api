"""Superadmin-only override and restore-authorization endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.superadmin_ops import service
from cbpupsis_api_admin.domains.superadmin_ops.schemas import (
    OverrideRequest,
    OverrideResult,
    RestoreAuthorizationRead,
)
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import (
    CurrentUser,
    require_superadmin_session,
)

router = APIRouter()


@router.post("/overrides/{rule_id}", response_model=OverrideResult)
async def perform_override(
    rule_id: str,
    data: OverrideRequest,
    user: CurrentUser = Depends(require_superadmin_session),
    db: AsyncSession = Depends(get_db),
) -> OverrideResult:
    return await service.perform_override(
        db,
        actor_id=user.id,
        rule_id=rule_id,
        target_id=data.target_id,
        justification=data.justification,
    )


@router.post(
    "/backups/{backup_id}/restore-requests",
    response_model=RestoreAuthorizationRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_restore(
    backup_id: uuid.UUID,
    user: CurrentUser = Depends(require_superadmin_session),
    db: AsyncSession = Depends(get_db),
) -> RestoreAuthorizationRead:
    return await service.request_restore(db, actor_id=user.id, backup_id=backup_id)


@router.post(
    "/backups/{backup_id}/restore-requests/approve",
    response_model=RestoreAuthorizationRead,
)
async def approve_restore(
    backup_id: uuid.UUID,
    user: CurrentUser = Depends(require_superadmin_session),
    db: AsyncSession = Depends(get_db),
) -> RestoreAuthorizationRead:
    return await service.approve_restore(db, actor_id=user.id, backup_id=backup_id)
