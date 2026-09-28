"""HTTP layer for the audit domain: read the trail.

Read-only by design — there is no POST. Entries arrive through the event
subscriber, never from a client, because an audit trail anyone can write to
proves nothing.

Gated behind ``ReadAllAuditEntry`` rather than ``ManageIAM``: reading the trail
and reshaping authorization are different powers, and an auditor who should see
every grant must not thereby be able to make one.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.pagination import Page
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.audit import service as audit_service
from cbpupsis_shared.domains.audit.constants import READ_ALL_AUDIT_ENTRY
from cbpupsis_shared.domains.audit.schemas import AuditEntryRead
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.iam.dependencies import require_permission

router = APIRouter()


@router.get("", response_model=Page[AuditEntryRead])
async def list_audit_entries(
    action: str | None = Query(
        default=None, description="Filter to one event name, e.g. iam.group_created."
    ),
    actor_id: uuid.UUID | None = Query(
        default=None, description="Filter to changes made by one user."
    ),
    target_id: str | None = Query(
        default=None, description="Filter to changes made to one target."
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(require_permission(READ_ALL_AUDIT_ENTRY)),
    db: AsyncSession = Depends(get_db),
) -> Page[AuditEntryRead]:
    """List recorded administrative changes, newest first.

    The two questions this is built to answer are "what happened to this thing?"
    (``target_id``) and "what has this person been doing?" (``actor_id``); both
    are indexed.
    """
    page = await audit_service.list_entries(
        db,
        limit=limit,
        offset=offset,
        action=action,
        actor_id=actor_id,
        target_id=target_id,
    )
    return Page[AuditEntryRead](
        items=[AuditEntryRead.model_validate(row) for row in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )
