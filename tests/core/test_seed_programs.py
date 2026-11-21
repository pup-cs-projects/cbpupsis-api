"""Tests for the programs seed that lets a Superadmin create student accounts."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.curriculum import Program
from cbpupsis_shared.domains.curriculum import client as curriculum_client
from scripts.seed_programs import PROGRAMS, seed


@pytest.fixture
def script_sessions(db: AsyncSession, monkeypatch) -> None:
    """Give the script its own sessions on the test database, as in production.

    The test engine uses a StaticPool, so a fresh session sees the same data.
    """
    monkeypatch.setattr(
        "scripts.seed_programs.AsyncSessionLocal",
        lambda: AsyncSession(db.bind, expire_on_commit=False),
    )


async def _programs(db: AsyncSession) -> list[Program]:
    db.expire_all()
    return list(await db.scalars(select(Program).order_by(Program.code)))


@pytest.mark.usefixtures("script_sessions")
class TestSeedPrograms:
    async def test_seeds_every_program_as_active(self, db: AsyncSession) -> None:
        await seed()

        rows = await _programs(db)
        assert [(r.code, r.name, r.department) for r in rows] == sorted(
            (code, name, department) for code, name, department, _ in PROGRAMS
        )
        assert all(r.is_active for r in rows)
        assert all(isinstance(r.total_units_required, Decimal) for r in rows)

    async def test_seeded_codes_are_accepted_for_student_accounts(
        self, db: AsyncSession
    ) -> None:
        await seed()

        codes = [code for code, *_ in PROGRAMS]
        found = await curriculum_client.active_programs_by_code(db, codes)
        assert sorted(found) == sorted(codes)

    async def test_second_run_changes_nothing(self, db: AsyncSession) -> None:
        await seed()
        first = [(r.id, r.code, r.name) for r in await _programs(db)]

        await seed()

        assert [(r.id, r.code, r.name) for r in await _programs(db)] == first

    async def test_leaves_an_existing_program_as_it_is(self, db: AsyncSession) -> None:
        db.add(
            Program(
                code="bscs",
                name="Renamed locally",
                department="Computer Science",
                total_units_required=Decimal("140.0"),
                is_active=False,
            )
        )
        await db.commit()

        await seed()

        rows = await _programs(db)
        existing = next(r for r in rows if r.code.upper() == "BSCS")
        assert (existing.name, existing.is_active) == ("Renamed locally", False)
        assert len(rows) == len(PROGRAMS)
