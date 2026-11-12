from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.enrollment import client as enrollment
from cbpupsis_shared.domains.enrollment.schemas import OverloadRequestRead


async def list_requests(
    db: AsyncSession,
    *,
    position: str,
    department_id: str | None,
    college_id: str | None,
) -> list[OverloadRequestRead]:
    return await enrollment.list_overload_requests(
        db,
        position=position,
        department=department_id,
        college=college_id,
    )


async def decide_request(
    db: AsyncSession,
    *,
    overload_request_id: uuid.UUID,
    decision: str,
    remarks: str | None,
    reviewer_id: uuid.UUID,
    position: str,
    department_id: str | None,
    college_id: str | None,
    client_ip: str | None,
) -> OverloadRequestRead:
    return await enrollment.decide_overload_request(
        db,
        overload_request_id=overload_request_id,
        decision=decision,
        remarks=remarks,
        reviewer_id=reviewer_id,
        position=position,
        department=department_id,
        college=college_id,
        client_ip=client_ip,
    )
