"""Seed a small set of active degree programs for local and QA databases.

A Superadmin creating a student account names a program code, and the account is
refused with ``ACCOUNT_PROGRAM_UNKNOWN`` unless that code is an active row in
``programs``. The curriculum domain has no write path yet, so this script is how
a fresh database gets one.

Idempotent: a program whose code already exists is left exactly as it is, so a
row someone renamed or deactivated is never overwritten. Run with::

    uv run python -m scripts.seed_programs
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import func, select

from cbpupsis_database.models.curriculum import Program
from cbpupsis_database.session import AsyncSessionLocal

#: (code, name, department, total units required). Codes are stored upper-cased
#: because account creation matches them case-insensitively.
PROGRAMS: list[tuple[str, str, str, Decimal]] = [
    (
        "BSCS",
        "Bachelor of Science in Computer Science",
        "Computer Science",
        Decimal("150.0"),
    ),
    (
        "BSIT",
        "Bachelor of Science in Information Technology",
        "Information Technology",
        Decimal("156.0"),
    ),
]


async def seed() -> None:
    """Create each program in ``PROGRAMS`` whose code is not already present."""
    async with AsyncSessionLocal() as session:
        for code, name, department, units in PROGRAMS:
            exists = await session.scalar(
                select(Program.id).where(func.upper(Program.code) == code)
            )
            if exists is None:
                session.add(
                    Program(
                        code=code,
                        name=name,
                        department=department,
                        total_units_required=units,
                        is_active=True,
                    )
                )
            print(f"{'Found' if exists else 'Created'} {code}")
        await session.commit()
    print(f"Program seed complete: {', '.join(code for code, *_ in PROGRAMS)}")


if __name__ == "__main__":
    asyncio.run(seed())
