"""Data access for the curriculum domain. Reads only, today.

Transactions belong to the service: nothing here commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.curriculum import Course, Program


async def schedule_courses(
    db: AsyncSession, ids: Collection[uuid.UUID]
) -> list[Course]:
    """Read catalog details for the selected sections, including inactive courses.

    SQL::

        SELECT * FROM courses WHERE id IN (:ids);
    """
    if not ids:
        return []
    return list(await db.scalars(select(Course).where(Course.id.in_(ids))))


async def list_active_programs_by_code(
    db: AsyncSession, codes: Collection[str]
) -> list[Program]:
    """Return the active programs whose code matches one of ``codes``.

    ``codes`` must already be upper-cased; the match ignores the stored case.

    SQL::

        SELECT programs.code, programs.name, programs.department,
               programs.total_units_required, programs.is_active, programs.id
        FROM programs
        WHERE upper(programs.code) IN (__[POSTCOMPILE_upper_1])
          AND programs.is_active IS true
    """
    if not codes:
        return []
    result = await db.scalars(
        select(Program).where(
            func.upper(Program.code).in_(list(codes)), Program.is_active.is_(True)
        )
    )
    return list(result)
