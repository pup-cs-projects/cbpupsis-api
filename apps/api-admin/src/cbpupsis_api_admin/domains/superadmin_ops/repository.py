"""Locked persistence access for restore authorization."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.superadmin_ops import SystemBackup


async def lock_backup(db: AsyncSession, backup_id: uuid.UUID) -> SystemBackup | None:
    """Lock the addressed backup, refreshing cached state.

    SQL::

        SELECT * FROM system_backups WHERE id=:backup_id FOR UPDATE
    """
    return await db.scalar(
        select(SystemBackup)
        .where(SystemBackup.id == backup_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
