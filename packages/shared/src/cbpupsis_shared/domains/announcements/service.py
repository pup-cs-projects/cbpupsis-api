"""Business logic for the announcements domain."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.announcements import repository
from cbpupsis_shared.domains.announcements.schemas import AnnouncementRead


async def list_active_announcements(
    db: AsyncSession, limit: int = 10
) -> list[AnnouncementRead]:
    rows = await repository.list_active_announcements(db, limit=limit)
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
