"""Business logic for the announcements domain."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.announcements import repository
from cbpupsis_shared.domains.announcements.schemas import AnnouncementRead
from cbpupsis_shared.domains.iam import service as iam_service


async def list_active_announcements(
    db: AsyncSession,
    limit: int = 10,
    *,
    role_code: str | None = None,
) -> list[AnnouncementRead]:
    target_role_id: uuid.UUID | None = None
    if role_code is not None:
        target_role_id = await iam_service.get_role_id_by_code(db, role_code)
    rows = await repository.list_active_announcements(
        db, limit=limit, target_role_id=target_role_id
    )
    return [
        AnnouncementRead(
            id=f"ann_{str(r.id)[:8]}",
            title=r.title,
            body=r.content,
            category=(r.category or "ACADEMIC").upper(),
            published_at=r.published_at,
            expires_at=r.expires_at,
            is_pinned=r.is_pinned,
        )
        for r in rows
    ]
