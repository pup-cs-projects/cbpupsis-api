"""Internal client for the announcements domain: the extraction swap point."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.announcements import service
from cbpupsis_shared.domains.announcements.schemas import AnnouncementRead

__all__ = [
    "AnnouncementRead",
    "list_active_announcements",
]


async def list_active_announcements(
    db: AsyncSession, limit: int = 10
) -> list[AnnouncementRead]:
    return await service.list_active_announcements(db, limit=limit)
