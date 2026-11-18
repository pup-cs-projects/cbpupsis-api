"""Opt-in #103 proof against an explicitly verified disposable local database.

ISSUE103_TEST_DATABASE_URL must name issue103_disposable[_suffix] on 127.0.0.1:55443.
Never set it to a development, shared, or remote database. Refuse nonempty reuse.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.curriculum import Course
from cbpupsis_database.models.grading import (
    GradeCorrection,
    StudentGrade,
    StudentTermGPA,
)
from cbpupsis_database.models.users import User
from cbpupsis_database.runtime import inspect_runtime
from cbpupsis_database.session import to_asyncpg_url
from cbpupsis_shared.domains.grading import corrections
from cbpupsis_shared.domains.grading import service as grades
from cbpupsis_shared.domains.grading.correction_schemas import (
    CorrectionDecision,
    CorrectionRead,
)
from cbpupsis_shared.domains.grading.exceptions import GradeVersionConflictError
from cbpupsis_shared.domains.grading.schemas import (
    FinalizeGradesRequest,
    SaveGradeDraftRequest,
)
from tests.domains.grading.test_grade_corrections import sheet
from tests.domains.schedule.helpers import seed_schedule


def verified_url(env_name="ISSUE103_TEST_DATABASE_URL"):
    url = os.environ.get(env_name)
    if not url:
        pytest.skip(
            "Requires an explicitly verified disposable #103 PostgreSQL database"
        )
    parsed = urlsplit(url)
    assert parsed.scheme == "postgresql" and parsed.hostname == "127.0.0.1"
    assert parsed.port == 55443 and (
        parsed.path == "/issue103_disposable"
        or parsed.path.startswith("/issue103_disposable_")
    )
    assert parsed.username == "issue103_owner" and not parsed.query
    return url


def migrate(url, direction, target):
    env = {
        **os.environ,
        "DATABASE_URL": url,
        "DIRECT_DATABASE_URL": url,
        "AUDIT_RUNTIME_ROLE": "issue103_runtime",
        "ENVIRONMENT": "development",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "packages/migrations/alembic.ini",
            direction,
            target,
        ],
        cwd=Path(__file__).resolve().parents[3],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-5000:]


async def ensure_runtime_role(connection):
    if not await connection.scalar(
        text("SELECT 1 FROM pg_roles WHERE rolname='issue103_runtime'")
    ):
        await connection.execute(
            text(
                "CREATE ROLE issue103_runtime LOGIN "
                "PASSWORD 'issue103-runtime-local-only' "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            )
        )


async def seed_record(db):
    data = await seed_schedule(db, "!synthetic-no-login", course_count=2)
    now = datetime.now(UTC)
    source = await db.get(AcademicTerm, data.term_id)
    source.term_type = "REGULAR"
    source.end_date = now.date() - timedelta(days=1)
    source.grade_submission_deadline = now + timedelta(days=1)
    next_start = now.date() + timedelta(days=5)
    db.add(
        AcademicTerm(
            academic_year="2027-2028",
            semester="first",
            term_type="REGULAR",
            start_date=next_start,
            end_date=next_start + timedelta(days=150),
            regular_enrollment_start=now,
            regular_enrollment_end=now,
            add_drop_deadline=now,
            grade_submission_deadline=now,
        )
    )
    for course in await db.scalars(select(Course)):
        course.department, course.college = "CS", "CCIS"
    actors = {}
    for position in ("chairperson", "dean", "registrar", "registrar2"):
        user = User(email=f"pg103-{position}@example.test", password_hash="!synthetic")
        db.add(user)
        await db.flush()
        actors[position] = user.id
    await db.commit()
    for index, section_id in enumerate(data.section_ids):
        initial = await grades.get_section_grades(
            db, faculty_id=data.faculty_id, section_id=section_id
        )
        draft = await grades.save_section_grade_draft(
            db,
            faculty_id=data.faculty_id,
            section_id=section_id,
            payload=SaveGradeDraftRequest(
                version=initial.version,
                grades=[
                    {
                        "enrollmentId": str(data.registration_ids[index]),
                        "finalGrade": "2.00" if index == 0 else "3.00",
                    }
                ],
            ),
        )
        await grades.finalize_section_grades(
            db,
            faculty_id=data.faculty_id,
            section_id=section_id,
            payload=FinalizeGradesRequest(version=draft.version),
        )
    db.add(
        StudentTermGPA(
            student_id=data.student_id,
            academic_term_id=data.term_id,
            total_gpa_units=Decimal("6"),
            term_gpa=Decimal("2.50"),
            cumulative_gpa=Decimal("2.50"),
            academic_standing="Warning Level 1",
        )
    )
    await db.commit()
    return data, actors


async def snapshot(db, data):
    marks = list(
        await db.scalars(
            select(StudentGrade.final_grade)
            .where(StudentGrade.enrollment_item_id.in_(data.registration_ids))
            .order_by(StudentGrade.enrollment_item_id)
        )
    )
    gpas = (
        await db.execute(
            select(StudentTermGPA.term_gpa, StudentTermGPA.cumulative_gpa).where(
                StudentTermGPA.student_id == data.student_id
            )
        )
    ).one()
    return marks, tuple(gpas)


async def test_ac_fac_002_8_postgres_audit_atomicity_and_concurrent_decisions(
    monkeypatch,
):
    url = verified_url()
    owner = create_async_engine(to_asyncpg_url(url))
    parsed = urlsplit(url)
    runtime_url = (
        "postgresql://issue103_runtime:issue103-runtime-local-only@"
        f"{parsed.hostname}:{parsed.port}{parsed.path}"
    )
    runtime = create_async_engine(to_asyncpg_url(runtime_url))
    owner_sessions = async_sessionmaker(owner, expire_on_commit=False)
    sessions = async_sessionmaker(runtime, expire_on_commit=False)
    try:
        async with owner.begin() as connection:
            assert await connection.scalar(
                text("SELECT current_database()")
            ) == parsed.path.removeprefix("/")
            assert (
                await connection.scalar(text("SELECT current_user")) == "issue103_owner"
            )
            assert not await connection.scalar(
                text("SELECT to_regclass('public.student_grades')")
            ), "Nonempty database: refusing destructive reuse"
            await ensure_runtime_role(connection)
        await asyncio.to_thread(migrate, url, "upgrade", "head")
        await asyncio.to_thread(
            migrate, url, "downgrade", "f177_section_grade_submissions"
        )
        await asyncio.to_thread(migrate, url, "upgrade", "head")
        async with runtime.connect() as connection:
            assert await inspect_runtime(connection, "issue103_runtime") == []
            assert (
                await connection.execute(text("SELECT current_user, session_user"))
            ).one() == ("issue103_runtime", "issue103_runtime")
        async with owner_sessions() as db:
            data, actors = await seed_record(db)

        async def action(db, row, position, action):
            return await corrections.decide_correction(
                db,
                row.id,
                actor_id=actors[position],
                position="registrar" if position == "registrar2" else position,
                department_id="CS",
                college_id="CCIS",
                decision=CorrectionDecision(action=action, version=row.version),
                client_ip="192.0.2.103",
            )

        async with sessions() as db:
            row = await corrections.file_correction(
                db,
                faculty_id=data.faculty_id,
                section_id=data.section_ids[0],
                student_id=data.student_id,
                proposed_mark="1.00",
                reason="Synthetic PostgreSQL evidence",
                content=sheet("PDF"),
                client_ip="192.0.2.103",
            )
            row = await action(db, row, "chairperson", "ENDORSE")
            row = await action(db, row, "dean", "ENDORSE")
            before = await snapshot(db, data)
            original = corrections._refresh_gpas

            async def fail_after_gpa(*args):
                await original(*args)
                raise RuntimeError("synthetic failure after mark and GPA flush")

            with monkeypatch.context() as patch:
                patch.setattr(corrections, "_refresh_gpas", fail_after_gpa)
                with pytest.raises(RuntimeError):
                    await action(db, row, "registrar", "COMMIT")
            assert await snapshot(db, data) == before
            saved = await db.get(GradeCorrection, row.id)
            assert (saved.state, saved.version) == ("PENDING_REGISTRAR", 3)
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(AuditEntry)
                    .where(AuditEntry.target_id == str(row.id))
                )
                == 3
            )

        # Both read the same initial version before either takes the student lock.
        barrier = asyncio.Event()
        arrived = 0
        original_visible = corrections._visible

        async def synchronized_visible(*args, **kwargs):
            nonlocal arrived
            result = await original_visible(*args, **kwargs)
            if not kwargs.get("lock"):
                arrived += 1
                if arrived == 2:
                    barrier.set()
                await asyncio.wait_for(barrier.wait(), timeout=15)
            return result

        async def commit(position):
            async with sessions() as db:
                return await action(db, row, position, "COMMIT")

        with monkeypatch.context() as patch:
            patch.setattr(corrections, "_visible", synchronized_visible)
            results = await asyncio.gather(
                commit("registrar"), commit("registrar2"), return_exceptions=True
            )
        assert sum(isinstance(result, CorrectionRead) for result in results) == 1, (
            results
        )
        assert (
            sum(isinstance(result, GradeVersionConflictError) for result in results)
            == 1
        ), results
        async with sessions() as db:
            marks, gpas = await snapshot(db, data)
            assert sorted(marks) == ["1.00", "3.00"]
            assert gpas == (Decimal("2.00"), Decimal("2.00"))
            entries = list(
                await db.scalars(
                    select(AuditEntry).where(AuditEntry.target_id == str(row.id))
                )
            )
            assert len(entries) == 4
            audit_id = entries[0].id
        # Real SQL using an actual restricted LOGIN, not owner/SET ROLE/mock errors.
        for statement in (
            "UPDATE audit_entries SET action='tampered' WHERE id=:id",
            "DELETE FROM audit_entries WHERE id=:id",
        ):
            async with runtime.connect() as connection:
                with pytest.raises(DBAPIError) as refused:
                    await connection.execute(text(statement), {"id": audit_id})
                assert "permission denied" in str(refused.value).lower()
                await connection.rollback()
        async with sessions() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(AuditEntry)
                    .where(AuditEntry.target_id == str(row.id))
                )
                == 4
            )
    finally:
        await runtime.dispose()
        await owner.dispose()


async def test_migration_preserves_legacy_correction_without_fabricated_evidence():
    url = verified_url("ISSUE103_LEGACY_DATABASE_URL")
    owner = create_async_engine(to_asyncpg_url(url))
    sessions = async_sessionmaker(owner, expire_on_commit=False)
    legacy_id = uuid.uuid4()
    try:
        async with owner.begin() as connection:
            assert not await connection.scalar(
                text("SELECT to_regclass('public.student_grades')")
            )
            await ensure_runtime_role(connection)
        await asyncio.to_thread(
            migrate, url, "upgrade", "f177_section_grade_submissions"
        )
        async with sessions() as db:
            data, _actors = await seed_record(db)
            grade_id = await db.scalar(
                select(StudentGrade.id).where(
                    StudentGrade.enrollment_item_id == data.registration_ids[0]
                )
            )
            await db.execute(
                text("""
                INSERT INTO grade_corrections
                (id, grade_id, faculty_id, current_mark, proposed_mark, reason,
                 grading_sheet_file_id, state, version)
                VALUES (:id, :grade, :faculty, '2.00', '1.25', 'Synthetic legacy',
                        'existing-private-reference', 'PENDING_CHAIR', 1)
            """),
                {"id": legacy_id, "grade": grade_id, "faculty": data.faculty_id},
            )
            before_audits = await db.scalar(
                select(func.count()).select_from(AuditEntry)
            )
            await db.commit()
        await asyncio.to_thread(migrate, url, "upgrade", "head")
        async with sessions() as db:
            row = await db.get(GradeCorrection, legacy_id)
            assert (
                row.section_id == data.section_ids[0]
                and row.student_id == data.student_id
            )
            assert row.grading_sheet_file_id == "existing-private-reference"
            assert row.window_closed_at is None and row.grade_updated_at is None
            assert row.grading_sheet_size is None and row.grading_sheet_sha256 is None
            assert (
                await db.scalar(
                    select(GradeCorrection.grading_sheet_content).where(
                        GradeCorrection.id == legacy_id
                    )
                )
                is None
            )
            assert (
                await db.scalar(select(func.count()).select_from(AuditEntry))
                == before_audits
            )
    finally:
        await owner.dispose()
