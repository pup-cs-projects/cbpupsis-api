"""Opt-in #100 PostgreSQL engineering check on a dedicated disposable database.

COR_TEST_DATABASE_URL must name local cor_issue100[_suffix]; never a shared DB.
COR_LEGACY_TEST_DATABASE_URL selects a separate fresh cor_issue100_* database.
Real producer workflows remain separate integration gates, not simulated here.
"""

import asyncio
import base64
import os
import subprocess
import sys
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cbpupsis_core.config import settings
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.billing import FeeStructure
from cbpupsis_database.models.curriculum import Course
from cbpupsis_database.models.enrollment import CorIssuance, TermEnrollment
from cbpupsis_database.models.sections import SectionSchedule
from cbpupsis_database.models.users import StudentProfile
from cbpupsis_database.runtime import inspect_runtime
from cbpupsis_database.session import to_asyncpg_url
from cbpupsis_shared.domains.billing import client as billing
from cbpupsis_shared.domains.enrollment import academic_verdict, certificate
from cbpupsis_shared.domains.enrollment.exceptions import CertificateUnavailable
from tests.domains.schedule.helpers import seed_schedule

URL = os.environ.get("COR_TEST_DATABASE_URL")
LEGACY_URL = os.environ.get("COR_LEGACY_TEST_DATABASE_URL")


def migrate(direction, target, *, url=None, expected_failure=None):
    env = {
        **os.environ,
        "DATABASE_URL": url or URL,
        "DIRECT_DATABASE_URL": url or URL,
        "AUDIT_RUNTIME_ROLE": "cor_runtime",
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
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if expected_failure:
        assert result.returncode != 0
        assert expected_failure in result.stderr, result.stderr[-1500:]
    else:
        assert result.returncode == 0, result.stderr[-1500:]


@pytest.mark.skipif(not URL, reason="requires isolated COR_TEST_DATABASE_URL")
async def test_cor_postgresql_migration_concurrency_snapshot_and_history(monkeypatch):
    from sqlalchemy.engine import make_url

    parsed = make_url(URL)
    assert parsed.host in {"localhost", "127.0.0.1"}
    assert parsed.database == "cor_issue100" or parsed.database.startswith(
        "cor_issue100_"
    )
    owner = create_async_engine(to_asyncpg_url(URL))
    runtime = create_async_engine(
        to_asyncpg_url(
            str(
                parsed.set(
                    username="cor_runtime", password="cor_runtime_local_only"
                ).render_as_string(hide_password=False)
            )
        )
    )
    owner_sessions = async_sessionmaker(owner, expire_on_commit=False)
    sessions = async_sessionmaker(runtime, expire_on_commit=False)
    try:
        async with owner.begin() as connection:
            assert not await connection.scalar(
                text("SELECT to_regclass('public.cor_issuances')")
            ), "Database is not empty; refusing reuse"
            if not await connection.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname='cor_runtime'")
            ):
                await connection.execute(
                    text(
                        "CREATE ROLE cor_runtime LOGIN "
                        "PASSWORD 'cor_runtime_local_only'"
                    )
                )
        await asyncio.to_thread(migrate, "upgrade", "head")
        await asyncio.to_thread(migrate, "downgrade", "-1")
        await asyncio.to_thread(migrate, "upgrade", "head")
        async with runtime.connect() as connection:
            assert await inspect_runtime(connection, "cor_runtime") == []
        monkeypatch.setattr(
            settings,
            "cor_encryption_key",
            SecretStr(base64.urlsafe_b64encode(bytes(range(32))).decode()),
        )
        monkeypatch.setattr(
            settings, "frontend_base_url_student", "https://student.example.test"
        )
        async with owner_sessions() as db:
            data = await seed_schedule(db, "!synthetic-no-login")
            profile = await db.get(StudentProfile, data.student_id)
            db.add(
                FeeStructure(
                    program_id=profile.program_id,
                    academic_term_id=data.term_id,
                    tuition_per_unit=Decimal("100.00"),
                )
            )
            await db.execute(
                update(TermEnrollment).values(enrollment_status="ENROLLED_PENDING")
            )
            await billing.stage_assessment(
                db,
                student_id=data.student_id,
                term_id=data.term_id,
                courses=[
                    billing.AssessmentCourse(section_id=s, units=Decimal(3))
                    for s in data.section_ids
                ],
                due_date=date(2026, 12, 1),
            )
            await db.commit()

        edited = False

        async def verdict(*args, **kwargs):
            nonlocal edited
            if not edited:
                edited = True
                # Source edits after issue's enrollment/profile snapshot must
                # not mix into its later catalogue/meeting reads.
                async with owner_sessions() as writer:
                    await writer.execute(
                        update(Course).values(title="Concurrent catalogue edit")
                    )
                    await writer.execute(
                        update(SectionSchedule).values(room="NEW ROOM")
                    )
                    await writer.commit()
            return True  # Explicit substitute; NOT real #99 eligibility proof.

        monkeypatch.setattr(academic_verdict, "certificate_eligible", verdict)

        async def issue():
            async with sessions() as db:
                return await certificate.issue_certificate(
                    db, data.student_id, data.term_id
                )

        first, second = await asyncio.gather(issue(), issue())
        assert first == second
        async with sessions() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(CorIssuance)
                    .where(CorIssuance.status == "CURRENT")
                )
                == 1
            )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(AuditEntry)
                    .where(AuditEntry.action == "enrollment.cor_issued")
                )
                == 1
            )
            dto = await certificate.read_certificate(db, data.student_id, data.term_id)
            assert dto.courses[0].title != "Concurrent catalogue edit"
            assert all(m.room != "NEW ROOM" for c in dto.courses for m in c.schedule)
            old_id = dto.certificateId
            # Existing index rejects a second CURRENT even outside the service.
            row = await db.get(CorIssuance, old_id)
            db.add(
                CorIssuance(
                    student_id=data.student_id,
                    term_enrollment_id=data.enrollment_id,
                    verification_token_hash="f" * 64,
                    token_envelope=row.token_envelope,
                    snapshot_envelope=row.snapshot_envelope,
                )
            )
            with pytest.raises(DBAPIError):
                await db.flush()
            await db.rollback()
            with pytest.raises(DBAPIError):
                await db.execute(
                    update(CorIssuance)
                    .where(CorIssuance.id == old_id)
                    .values(snapshot_envelope="tampered")
                )
            await db.rollback()
            # Reissue requires the approval owner to start coherent isolation.
            stored = await billing.read_stored_assessment(
                db, data.student_id, data.term_id
            )
            with pytest.raises(CertificateUnavailable):
                await certificate.stage_reissue(
                    db,
                    student_id=data.student_id,
                    term_id=data.term_id,
                    actor_id=data.faculty_id,
                    assessment=stored,
                )
            await db.rollback()
            await certificate.repository.begin_certificate_snapshot(db)
            new = await certificate.stage_reissue(
                db,
                student_id=data.student_id,
                term_id=data.term_id,
                actor_id=data.faculty_id,
                assessment=stored,
            )
            await db.commit()
            assert new.certificateId != old_id
            assert (
                await certificate.verify_certificate(
                    db, dto.verificationUrl.split("/")[-1]
                )
            ).status == "SUPERSEDED"
            assert (await db.get(CorIssuance, old_id)).issued_at == dto.issuedAt
            # Race on an already-official parent (no parent status UPDATE) must
            # also retry an index conflict and preserve one new current COR.
            await db.execute(
                update(CorIssuance)
                .where(CorIssuance.status == "CURRENT")
                .values(status="SUPERSEDED", superseded_at=datetime.now(UTC))
            )
            await db.commit()

        async def eligible(*args, **kwargs):
            await asyncio.sleep(0.1)  # Let the other issuer wait on the parent lock.
            return True

        monkeypatch.setattr(academic_verdict, "certificate_eligible", eligible)
        first, second = await asyncio.gather(issue(), issue())
        assert first == second
        async with sessions() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(CorIssuance)
                    .where(CorIssuance.status == "CURRENT")
                )
                == 1
            )
    finally:
        await runtime.dispose()
        await owner.dispose()


@pytest.mark.skipif(
    not LEGACY_URL, reason="requires fresh COR_LEGACY_TEST_DATABASE_URL"
)
async def test_cor_postgresql_legacy_rows_and_duplicate_migration_preflight():
    from sqlalchemy.engine import make_url

    parsed = make_url(LEGACY_URL)
    assert parsed.host in {"localhost", "127.0.0.1"}
    assert parsed.database.startswith("cor_issue100_")
    owner = create_async_engine(to_asyncpg_url(LEGACY_URL))
    sessions = async_sessionmaker(owner, expire_on_commit=False)
    try:
        async with owner.begin() as connection:
            assert not await connection.scalar(
                text("SELECT to_regclass('public.cor_issuances')")
            ), "Database is not empty; refusing reuse"
            if not await connection.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname='cor_runtime'")
            ):
                await connection.execute(
                    text(
                        "CREATE ROLE cor_runtime LOGIN "
                        "PASSWORD 'cor_runtime_local_only'"
                    )
                )
        # Seed through the current models, then step back to the revision before
        # e100 so the raw inserts below are genuine pre-certificate rows.
        await asyncio.to_thread(migrate, "upgrade", "head", url=LEGACY_URL)
        async with sessions() as db:
            data = await seed_schedule(db, "!synthetic-no-login")
            await db.commit()
        await asyncio.to_thread(
            migrate, "downgrade", "e099_academic_waivers", url=LEGACY_URL
        )
        async with sessions() as db:
            legacy_ids = [uuid.uuid4(), uuid.uuid4()]
            for row_id, digest in zip(legacy_ids, ("a" * 64, "b" * 64), strict=True):
                await db.execute(
                    text("""
                    INSERT INTO cor_issuances
                        (id, student_id, term_enrollment_id, verification_token_hash)
                    VALUES (:id, :student, :enrollment, :digest)
                """),
                    {
                        "id": row_id,
                        "student": data.student_id,
                        "enrollment": data.enrollment_id,
                        "digest": digest,
                    },
                )
            await db.commit()
        await asyncio.to_thread(
            migrate,
            "upgrade",
            "head",
            url=LEGACY_URL,
            expected_failure="Duplicate current CORs require owner reconciliation",
        )
        async with sessions() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version"))
                == "e099_academic_waivers"
            )
            assert await db.scalar(text("SELECT count(*) FROM cor_issuances")) == 2
            assert not await db.scalar(
                text("""
                SELECT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name='cor_issuances' AND column_name='token_envelope')
            """)
            )
            # Explicit synthetic owner decision, NOT automatic production repair.
            await db.execute(
                text("""
                UPDATE cor_issuances SET status='SUPERSEDED', superseded_at=now()
                WHERE id=:id
            """),
                {"id": legacy_ids[1]},
            )
            await db.commit()
        await asyncio.to_thread(migrate, "upgrade", "head", url=LEGACY_URL)
        async with sessions() as db:
            rows = list(await db.scalars(select(CorIssuance).order_by(CorIssuance.id)))
            assert len(rows) == 2 and {r.id for r in rows} == set(legacy_ids)
            assert {r.verification_token_hash for r in rows} == {"a" * 64, "b" * 64}
            assert all(
                r.token_envelope is None and r.snapshot_envelope is None for r in rows
            )
            with pytest.raises(CertificateUnavailable):
                await certificate.read_certificate(db, data.student_id, data.term_id)
            with pytest.raises(CertificateUnavailable):
                await certificate.issue_certificate(db, data.student_id, data.term_id)
            assert await db.scalar(select(func.count()).select_from(CorIssuance)) == 2
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(AuditEntry)
                    .where(AuditEntry.action == "enrollment.cor_issued")
                )
                == 0
            )
    finally:
        await owner.dispose()
