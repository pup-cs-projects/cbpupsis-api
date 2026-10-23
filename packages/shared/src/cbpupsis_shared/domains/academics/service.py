"""Internal term metadata service; no public term-management API."""

import uuid
from collections.abc import Collection

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.academics import repository
from cbpupsis_shared.domains.academics.schemas import TermRead


async def record_terms(db: AsyncSession, ids: Collection[uuid.UUID]) -> list[TermRead]:
    return [
        TermRead.model_validate(row) for row in await repository.list_terms(db, ids)
    ]
