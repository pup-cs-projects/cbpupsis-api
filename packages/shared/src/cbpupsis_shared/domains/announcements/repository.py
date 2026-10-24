"""Data access for the announcements domain.

Only this module builds SQL or calls into the session for campus announcements.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.announcements import Announcement


async def list_active_announcements(
    db: AsyncSession, *, limit: int = 10
) -> list[Announcement]:
    """Return active published announcements ordered by pinned then date.

    SQL::

        SELECT announcements.id, announcements.author_id,
               announcements.title, announcements.content,
               announcements.category, announcements.banner_image_url,
               announcements.target_role_id, announcements.is_pinned,
               announcements.is_published, announcements.published_at,
               announcements.expires_at, announcements.created_at
        FROM announcements
        WHERE announcements.is_published IS true
          AND (announcements.expires_at IS NULL
               OR announcements.expires_at > CURRENT_TIMESTAMP)
        ORDER BY announcements.is_pinned DESC,
                 announcements.published_at DESC
        LIMIT :limit
    """
    now = datetime.now(UTC)
    stmt = (
        select(Announcement)
        .where(
            Announcement.is_published.is_(True),
            or_(Announcement.expires_at.is_(None), Announcement.expires_at > now),
        )
        .order_by(Announcement.is_pinned.desc(), Announcement.published_at.desc())
        .limit(limit)
    )
    result = await db.scalars(stmt)
    return list(result)
