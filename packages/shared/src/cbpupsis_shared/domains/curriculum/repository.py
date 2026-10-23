"""Data access for the curriculum domain. Reads only, today.

Transactions belong to the service: nothing here commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.curriculum import Curriculum, Program


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


async def get_program(db: AsyncSession, program_id: uuid.UUID) -> Program | None:
    """Return a degree program by primary key, or ``None``.

    SQL::

        SELECT * FROM programs WHERE id = :program_id
    """
    return await db.get(Program, program_id)


async def get_curriculum(
    db: AsyncSession, curriculum_id: uuid.UUID
) -> Curriculum | None:
    """Return a curriculum version by primary key, or ``None``.

    SQL::

        SELECT * FROM curricula WHERE id = :curriculum_id
    """
    return await db.get(Curriculum, curriculum_id)
