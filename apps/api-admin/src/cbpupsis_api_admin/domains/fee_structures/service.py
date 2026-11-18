"""Admin fee structures service facade."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.billing import service as billing_service
from cbpupsis_shared.domains.billing.schemas import (
    FeeStructureRead,
    FeeStructureSaveRequest,
)


async def get_fee_structure_for_term(
    db: AsyncSession, term_id: uuid.UUID
) -> FeeStructureRead:
    return await billing_service.get_fee_structure_for_term(db, term_id)


async def save_fee_structure(
    db: AsyncSession,
    *,
    term_id: uuid.UUID,
    payload: FeeStructureSaveRequest,
    actor_id: uuid.UUID,
    role: str,
    client_ip: str | None,
) -> FeeStructureRead:
    return await billing_service.save_fee_structure(
        db,
        term_id=term_id,
        payload=payload,
        actor_id=actor_id,
        role=role,
        client_ip=client_ip,
    )
