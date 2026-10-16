"""Business logic for the curriculum domain: programs and their curricula.

Only the program lookup account creation needs exists so far. Program and
curriculum administration is its own epic.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.curriculum import repository
from cbpupsis_shared.domains.curriculum.schemas import ProgramRead


async def active_programs_by_code(
    db: AsyncSession, codes: Iterable[str]
) -> dict[str, ProgramRead]:
    """Map each upper-cased code that names an active program to that program.

    Codes compare case-insensitively, so ``bscs`` and ``BSCS`` are one program.
    A code missing from the result is unknown or inactive.
    """
    wanted = {code.strip().upper() for code in codes if code and code.strip()}
    rows = await repository.list_active_programs_by_code(db, wanted)
    return {row.code.upper(): ProgramRead.model_validate(row) for row in rows}
