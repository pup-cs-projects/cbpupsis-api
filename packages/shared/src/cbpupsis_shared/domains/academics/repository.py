"""Batched term metadata reads."""

import uuid
from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.academics import AcademicTerm


async def list_terms(
    db: AsyncSession, ids: Collection[uuid.UUID]
) -> list[AcademicTerm]:
    """Read only terms referenced by the caller's record.

    SQL::

        SELECT * FROM academic_terms WHERE id IN (:ids);
    """
    if not ids:
        return []
    return list(await db.scalars(select(AcademicTerm).where(AcademicTerm.id.in_(ids))))
