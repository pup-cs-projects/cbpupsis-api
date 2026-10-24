"""Internal client for the curriculum domain: the extraction swap point.

Other domains depend on this module, never on ``service.py`` directly.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.curriculum import service
from cbpupsis_shared.domains.curriculum.schemas import ProgramRead, ScheduleCourse
from cbpupsis_shared.domains.curriculum.service import record_courses

__all__ = [
    "ProgramRead",
    "active_programs_by_code",
    "record_courses",
    "schedule_courses",
]


async def schedule_courses(
    db: AsyncSession, ids: list[uuid.UUID]
) -> list[ScheduleCourse]:
    return await service.schedule_courses(db, ids)


async def active_programs_by_code(
    db: AsyncSession, codes: Iterable[str]
) -> dict[str, ProgramRead]:
    return await service.active_programs_by_code(db, codes)
