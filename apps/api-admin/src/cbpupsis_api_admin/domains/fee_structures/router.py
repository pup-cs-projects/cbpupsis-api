"""Admin fee structures HTTP routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.admin_auth.dependencies import (
    AdminSession,
    require_admin_permission,
)
from cbpupsis_api_admin.domains.fee_structures import service
from cbpupsis_api_admin.domains.fee_structures.schemas import (
    FeeStructureRead,
    FeeStructureSaveRequest,
)
from cbpupsis_core.middleware import client_ip_or_none
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.iam.constants import MANAGE_FEE_STRUCTURE

router = APIRouter()


@router.get(
    "/{term_id}",
    response_model=FeeStructureRead,
    operation_id="get_admin_fee_structure",
)
async def get_fee_structure(
    term_id: uuid.UUID,
    admin: AdminSession = Depends(require_admin_permission(MANAGE_FEE_STRUCTURE)),
    db: AsyncSession = Depends(get_db),
) -> FeeStructureRead:
    """Read fee structure configuration for the specified academic term."""
    return await service.get_fee_structure_for_term(db, term_id)


@router.put(
    "/{term_id}",
    response_model=FeeStructureRead,
    operation_id="save_admin_fee_structure",
)
async def save_fee_structure(
    term_id: uuid.UUID,
    payload: FeeStructureSaveRequest,
    request: Request,
    admin: AdminSession = Depends(require_admin_permission(MANAGE_FEE_STRUCTURE)),
    db: AsyncSession = Depends(get_db),
) -> FeeStructureRead:
    """Save and validate fee structure configuration for the specified term."""
    return await service.save_fee_structure(
        db,
        term_id=term_id,
        payload=payload,
        actor_id=admin.id,
        role=admin.position,
        client_ip=client_ip_or_none(request),
    )
