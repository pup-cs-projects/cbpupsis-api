"""Opt-in #96 migration, snapshot, and concurrency checks on owned local data.

Set SCHEDULE_POSTGRES_URL to an owner login on a disposable database named
cbpupsis_schedule_test*. The fixture applies migrations and creates synthetic data.
Never falls back to DATABASE_URL or DIRECT_DATABASE_URL.
"""

import asyncio
import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from icalendar import Calendar
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from cbpupsis_api_student.domains.schedule import service
from cbpupsis_api_student.domains.schedule.exceptions import ScheduleUnavailableError
from cbpupsis_database.models.schedule import ScheduleExportState
from cbpupsis_database.models.sections import SectionSchedule
from cbpupsis_database.session import to_asyncpg_url
from cbpupsis_shared.domains.auth.security import hash_password
from tests.domains.schedule.helpers import PASSWORD, seed_schedule

URL = os.environ.get("SCHEDULE_POSTGRES_URL", "")
ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    not URL, reason="Requires explicit disposable SCHEDULE_POSTGRES_URL"
)


@pytest.fixture(scope="module")
def migrated_url():
    parts = urlsplit(URL)
    assert parts.hostname in {"127.0.0.1", "localhost", "::1"}
    assert parts.path.removeprefix("/").startswith("cbpupsis_schedule_test")
    env = {
        **os.environ,
        "DATABASE_URL": URL,
        "DIRECT_DATABASE_URL": URL,
        "AUDIT_RUNTIME_ROLE": "schedule_runtime",
        "DEBUG": "false",
    }
    password = secrets.token_urlsafe(24)

    async def create_runtime():
        engine = create_async_engine(to_asyncpg_url(URL), poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                # This is an explicitly owned disposable cluster, not a shared role.
                await connection.execute(
                    text(f"CREATE ROLE schedule_runtime LOGIN PASSWORD '{password}'")
                )
        finally:
            await engine.dispose()

    asyncio.run(create_runtime())
    for operation in ("upgrade head", "downgrade -1", "upgrade head"):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                "packages/migrations/alembic.ini",
                *operation.split(),
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr[-3000:]
    runtime = make_url(URL).set(username="schedule_runtime", password=password)
    return URL, runtime.render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def seeded(migrated_url):
    async def seed():
        engine = create_async_engine(
            to_asyncpg_url(migrated_url[0]), poolclass=NullPool
        )
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                return await seed_schedule(db, hash_password(PASSWORD))
        finally:
            await engine.dispose()

    return asyncio.run(seed())


async def test_ac_103_8_concurrent_exports_and_runtime_grants(migrated_url, seeded):
    engine = create_async_engine(to_asyncpg_url(migrated_url[1]), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:

        async def export():
            async with factory() as db:
                content = await service.export_schedule(
                    db, student_id=seeded.student_id, format="ics"
                )
                return [
                    (str(event["uid"]), int(event["sequence"]))
                    for event in Calendar.from_ical(content).walk("VEVENT")
                ]

        results = await asyncio.gather(*(export() for _ in range(8)))
        assert all(result == results[0] for result in results)
        assert len(results[0]) == 4 and all(sequence == 1 for _, sequence in results[0])
        async with factory() as db:
            assert (await db.scalar(select(ScheduleExportState))).sequence == 1
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version"))
                == "d096_schedule_export"
            )
    finally:
        await engine.dispose()


async def test_ac_103_1_multi_domain_reads_share_a_snapshot(
    migrated_url, seeded, monkeypatch
):
    engine = create_async_engine(to_asyncpg_url(migrated_url[1]), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    original = service.enrollment.enrolled_sections
    try:

        async def race(db, student_id, term_id):
            rows = await original(db, student_id, term_id)
            async with factory() as writer:
                await writer.execute(
                    update(SectionSchedule)
                    .where(SectionSchedule.id == seeded.meeting_ids[0])
                    .values(room="CHANGED")
                )
                await writer.commit()
            return rows

        monkeypatch.setattr(service.enrollment, "enrolled_sections", race)
        async with factory() as db:
            schedule = await service.read_schedule(db, student_id=seeded.student_id)
            assert schedule.courses[0].meetings[0].room == "R101"
        monkeypatch.setattr(service.enrollment, "enrolled_sections", original)
        async with factory() as db:
            schedule = await service.read_schedule(db, student_id=seeded.student_id)
            assert schedule.courses[0].meetings[0].room == "CHANGED"
    finally:
        await engine.dispose()


async def test_export_lock_deadline_releases_transaction_and_recovers(
    migrated_url, seeded, monkeypatch
):
    engine = create_async_engine(to_asyncpg_url(migrated_url[1]), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as holder, factory() as exporter:
            await service.repository.lock_export_state(
                holder, seeded.student_id, seeded.term_id
            )
            monkeypatch.setattr(service, "EXPORT_TIMEOUT_SECONDS", 0.2)
            with pytest.raises(ScheduleUnavailableError):
                await service.export_schedule(
                    exporter, student_id=seeded.student_id, format="ics"
                )
            assert not exporter.in_transaction()
            assert await exporter.scalar(text("SELECT 1")) == 1
            await exporter.rollback()
            await holder.rollback()
            monkeypatch.setattr(service, "EXPORT_TIMEOUT_SECONDS", 10)
            content = await service.export_schedule(
                exporter, student_id=seeded.student_id, format="ics"
            )
            assert len(Calendar.from_ical(content).walk("VEVENT")) == 4
    finally:
        await engine.dispose()
