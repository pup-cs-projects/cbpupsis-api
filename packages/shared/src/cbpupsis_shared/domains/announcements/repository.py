"""Data access for the announcements domain.

Only this module builds SQL or calls into the session for campus announcements.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.announcements import Announcement


async def list_active_announcements(
    db: AsyncSession,
    *,
    limit: int = 10,
    target_role_id: uuid.UUID | None = None,
) -> list[Announcement]:
    """Return active published announcements ordered by pinned then date.

    Includes global announcements (``target_role_id IS NULL``) and, when
    ``target_role_id`` is provided, announcements targeted to that role.

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
          AND (announcements.target_role_id IS NULL
               OR announcements.target_role_id = :target_role_id)
        ORDER BY announcements.is_pinned DESC,
                 announcements.published_at DESC
        LIMIT :limit
    """
    now = datetime.now(UTC)
    role_filter = (
        Announcement.target_role_id.is_(None)
        if target_role_id is None
        else or_(
            Announcement.target_role_id.is_(None),
            Announcement.target_role_id == target_role_id,
        )
    )
    stmt = (
        select(Announcement)
        .where(
            Announcement.is_published.is_(True),
            or_(Announcement.expires_at.is_(None), Announcement.expires_at > now),
            role_filter,
        )
        .order_by(Announcement.is_pinned.desc(), Announcement.published_at.desc())
        .limit(limit)
    )
    result = await db.scalars(stmt)
    return list(result)
