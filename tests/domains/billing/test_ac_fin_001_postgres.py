"""Opt-in PostgreSQL proof on the explicitly verified disposable #106 database.

Set ISSUE106_TEST_DATABASE_URL only for the issue106_synthetic database at
127.0.0.1:55446. The harness refuses all other destinations before any write.
Migrate this database to head with its restricted issue106_runtime role first.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import event, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.billing import FeeStructure, StudentBillingAccount
from cbpupsis_database.models.curriculum import Program
from cbpupsis_database.models.outbox import OutboxMessage
from cbpupsis_database.models.users import StudentProfile, User
from cbpupsis_database.runtime import inspect_runtime
from cbpupsis_database.session import to_asyncpg_url
from cbpupsis_shared.domains.billing import client as billing
from cbpupsis_shared.domains.billing.account import stage_account_read
from cbpupsis_shared.domains.billing.assessment import refresh_current_subsidy
from cbpupsis_shared.domains.users import client as users


def _verified_url(
    env_name="ISSUE106_TEST_DATABASE_URL", database="issue106_synthetic"
) -> str:
    url = os.environ.get(env_name)
    if not url:
        pytest.skip("Requires the explicitly verified disposable issue106 database")
    parsed = urlsplit(url)
    assert parsed.scheme == "postgresql"
    assert parsed.hostname == "127.0.0.1" and parsed.port == 55446
    assert parsed.path == f"/{database}" and not parsed.query
    assert parsed.username == "issue106"
    return url


def _migrate(url: str, direction: str, target: str, *, expected_error=None) -> None:
    root = Path(__file__).resolve().parents[3]
    env = {
        **os.environ,
        "DATABASE_URL": url,
        "DIRECT_DATABASE_URL": url,
        "AUDIT_RUNTIME_ROLE": "issue106_runtime",
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
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    output = (result.stdout + result.stderr)[-6000:]
    if expected_error is not None:
        assert result.returncode != 0 and expected_error in output, output
    else:
        assert result.returncode == 0, output


async def _seed(db):
    now = datetime.now(UTC)
    program = Program(
        code=f"F106{uuid.uuid4().hex[:8]}",
        name="Synthetic finance integration",
        department="Computer Science",
        total_units_required=Decimal("140"),
    )
    term = AcademicTerm(
        academic_year=f"106-{uuid.uuid4().hex[:10]}",
        semester="First",
        start_date=date(2020, 1, 1),
        end_date=date(2099, 12, 1),
        is_current=True,
        regular_enrollment_start=now - timedelta(days=1),
        regular_enrollment_end=now + timedelta(days=1),
        add_drop_deadline=now + timedelta(days=7),
        grade_submission_deadline=now + timedelta(days=30),
    )
    student = User(
        email=f"{uuid.uuid4()}@example.com",
        password_hash="synthetic-no-login",
        is_active=True,
    )
    db.add_all([program, term, student])
    await db.flush()
    profile = StudentProfile(
        user_id=student.id,
        student_number=f"106-{uuid.uuid4().hex[:12]}",
        program_id=program.id,
        year_level=1,
        enrollment_status="PENDING",
    )
    db.add_all(
        [
            profile,
            FeeStructure(
                program_id=program.id,
                academic_term_id=term.id,
                tuition_per_unit=Decimal("100.11"),
                registration_fee=Decimal("50.01"),
                lab_fee=Decimal("20.02"),
                misc_fee=Decimal("10.03"),
                late_fee_rate=Decimal("0.00"),
                subsidy_covered_fees=["TUITION", "LAB", "MISC", "REGISTRATION"],
            ),
        ]
    )
    await db.flush()
    await billing.stage_assessment(
        db,
        student_id=student.id,
        term_id=term.id,
        courses=[
            billing.AssessmentCourse(section_id=uuid.uuid4(), units=Decimal("3.5"))
        ],
        due_date=date(2020, 1, 1),
    )
    await db.commit()
    return student.id, term.id, profile


async def _notice_count(db, student_id):
    return await db.scalar(
        select(func.count())
        .select_from(OutboxMessage)
        .where(
            OutboxMessage.event_name == "billing.account_overdue",
            OutboxMessage.payload["user_id"].as_string() == str(student_id),
        )
    )


async def test_ac_fin_001_7_9_postgres_concurrency_rollback_and_migration():
    url = _verified_url()
    owner_engine = create_async_engine(to_asyncpg_url(url))
    runtime_url = url.replace(
        "issue106:local_issue106_only@", "issue106_runtime:synthetic_runtime_only@"
    )
    assert urlsplit(runtime_url).username == "issue106_runtime"
    runtime_engine = create_async_engine(to_asyncpg_url(runtime_url), pool_size=8)
    owner_sessions = async_sessionmaker(owner_engine, expire_on_commit=False)
    runtime_sessions = async_sessionmaker(runtime_engine, expire_on_commit=False)
    term_id = None
    try:
        async with owner_engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT current_database()"))
                == "issue106_synthetic"
            )
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "c192_student_account_late_fine"
            )
        async with runtime_engine.connect() as connection:
            assert await inspect_runtime(connection, "issue106_runtime") == []
        async with owner_sessions() as db:
            assert not await db.scalar(
                select(AcademicTerm.id).where(AcademicTerm.is_current)
            )
            student_id, term_id, profile = await _seed(db)
            profile.fhe_forfeited = True
            profile.fhe_forfeiture_reason = "Approved synthetic academic decision"
            await db.commit()
        async with runtime_sessions() as db:
            staged = await stage_account_read(db, student_id)
            assert staged.paymentStatus == "OVERDUE"
            await db.flush()
            await db.rollback()
        async with owner_sessions() as db:
            account = await db.scalar(
                select(StudentBillingAccount).where(
                    StudentBillingAccount.student_id == student_id
                )
            )
            assert account.overdue_notified_at is None
            assert account.late_penalty_applied_at is None
            assert account.late_penalty_amount == 0
            assert account.fhe_subsidy_amount == Decimal("430.45")
            assert await _notice_count(db, student_id) == 0

        async def read():
            async with runtime_sessions() as db:
                result = await billing.read_account(db, student_id)
                assert result.netPayable == result.amountAssessed == "530.45"
                assert result.paymentStatus == "OVERDUE"
                assert result.lines[-1].amount == "100.00"
                assert result.lines[-1].coveredByFhe is False
                assert result.holds[0].amount_outstanding == "530.45"

        await asyncio.gather(*(read() for _ in range(8)))
        async with owner_sessions() as db:
            assert await _notice_count(db, student_id) == 1
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(AuditEntry)
                    .where(
                        AuditEntry.action == "billing.subsidy_withdrawn",
                        AuditEntry.payload["studentId"].as_string() == str(student_id),
                    )
                )
                == 1
            )
            account = await db.scalar(
                select(StudentBillingAccount).where(
                    StudentBillingAccount.student_id == student_id
                )
            )
            before_snapshot = account.assessment_snapshot
            before_id = account.id
            assert account.late_penalty_amount == Decimal("100.00")
            assert account.balance == Decimal("530.45")
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(AuditEntry)
                    .where(
                        AuditEntry.action == "billing.late_penalty_applied",
                        AuditEntry.payload["studentId"].as_string() == str(student_id),
                    )
                )
                == 1
            )

        await owner_engine.dispose()
        await runtime_engine.dispose()
        await asyncio.to_thread(
            _migrate,
            url,
            "downgrade",
            "c191_student_account_overdue",
            expected_error="Recorded late fines require explicit reconciliation",
        )
        async with owner_sessions() as db:
            account = await db.get(StudentBillingAccount, before_id)
            assert account.assessment_snapshot == before_snapshot
            assert account.overdue_notified_at is not None
            assert account.late_penalty_amount == Decimal("100.00")
            assert account.late_penalty_applied_at is not None
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version"))
                == "c192_student_account_late_fine"
            )
        await asyncio.gather(*(read() for _ in range(4)))
        async with owner_sessions() as db:
            assert await _notice_count(db, student_id) == 1
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(AuditEntry)
                    .where(
                        AuditEntry.action == "billing.late_penalty_applied",
                        AuditEntry.payload["studentId"].as_string() == str(student_id),
                    )
                )
                == 1
            )
        await _verify_stale_decision_race(
            owner_sessions, runtime_sessions, runtime_engine, student_id, term_id
        )
    finally:
        if term_id is not None:
            async with owner_sessions() as db:
                await db.execute(
                    update(AcademicTerm)
                    .where(AcademicTerm.id == term_id)
                    .values(is_current=False)
                )
                await db.commit()
        await owner_engine.dispose()
        await runtime_engine.dispose()


async def _verify_stale_decision_race(
    owner_sessions, runtime_sessions, runtime_engine, student_id, term_id
):
    # A queued request must not overwrite a newer approved decision with a DTO
    # captured before its account lock was acquired.
    async with runtime_sessions() as db:
        stale_profile = await users.get_student_profile(db, student_id)
    entered = asyncio.Event()

    def before_cursor_execute(_conn, _cursor, statement, _params, _ctx, _many):
        if "student_billing_accounts" in statement and "FOR UPDATE" in statement:
            entered.set()

    async def refresh_stale():
        async with runtime_sessions() as db:
            result = await refresh_current_subsidy(db, stale_profile, term_id)
            await db.commit()
            return result

    event.listen(
        runtime_engine.sync_engine, "before_cursor_execute", before_cursor_execute
    )
    waiter = None
    try:
        async with owner_sessions() as db:
            account = await db.scalar(
                select(StudentBillingAccount)
                .where(StudentBillingAccount.student_id == student_id)
                .with_for_update()
            )
            waiter = asyncio.create_task(refresh_stale())
            await asyncio.wait_for(entered.wait(), timeout=10)
            profile = await db.get(StudentProfile, student_id, populate_existing=True)
            profile.fhe_forfeited = False
            profile.fhe_forfeiture_reason = None
            await db.flush()
            stored = await billing.read_stored_assessment(db, student_id, term_id)
            await billing.stage_assessment(
                db,
                student_id=student_id,
                term_id=term_id,
                courses=stored.inputs.courses,
                due_date=account.due_date,
            )
            await db.commit()
        refreshed = await asyncio.wait_for(waiter, timeout=10)
        assert refreshed.assessment.subsidyApplied == "430.45"
        assert refreshed.assessment.fheForfeited is False
        async with owner_sessions() as db:
            account = await db.scalar(
                select(StudentBillingAccount).where(
                    StudentBillingAccount.student_id == student_id
                )
            )
            assert account.late_penalty_amount == account.balance == Decimal("100.00")
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(AuditEntry)
                    .where(
                        AuditEntry.action == "billing.subsidy_withdrawn",
                        AuditEntry.payload["studentId"].as_string() == str(student_id),
                    )
                )
                == 1
            )
    finally:
        event.remove(
            runtime_engine.sync_engine, "before_cursor_execute", before_cursor_execute
        )
        if waiter is not None and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)


async def test_ac_fin_001_7_postgres_zero_fine_migration_roundtrip():
    url = _verified_url(
        "ISSUE106_MIGRATION_DATABASE_URL", "issue106_synthetic_migration"
    )
    engine = create_async_engine(to_asyncpg_url(url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    term_id = None
    try:
        async with engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT current_database()"))
                == "issue106_synthetic_migration"
            )
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "c192_student_account_late_fine"
            )
            assert not await connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM student_billing_accounts "
                    "WHERE late_penalty_amount <> 0)"
                )
            )
        async with sessions() as db:
            student_id, term_id, _ = await _seed(db)
            account = await db.scalar(
                select(StudentBillingAccount).where(
                    StudentBillingAccount.student_id == student_id
                )
            )
            account.amount_paid = Decimal("0.01")
            await db.commit()
            before_id = account.id
            before_snapshot = account.assessment_snapshot
        await engine.dispose()
        await asyncio.to_thread(
            _migrate, url, "downgrade", "c191_student_account_overdue"
        )
        async with engine.connect() as connection:
            assert await connection.scalar(
                text("SELECT balance FROM student_billing_accounts WHERE id = :id"),
                {"id": before_id},
            ) == Decimal("-0.01")
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "c191_student_account_overdue"
            )
        await engine.dispose()
        await asyncio.to_thread(_migrate, url, "upgrade", "head")
        async with sessions() as db:
            account = await db.get(StudentBillingAccount, before_id)
            assert account.assessment_snapshot == before_snapshot
            assert account.balance == Decimal("-0.01")
            assert account.amount_paid == Decimal("0.01")
            assert (
                account.late_penalty_amount == 0
                and account.late_penalty_applied_at is None
            )
    finally:
        if term_id is not None:
            async with sessions() as db:
                await db.execute(
                    update(AcademicTerm)
                    .where(AcademicTerm.id == term_id)
                    .values(is_current=False)
                )
                await db.commit()
        await engine.dispose()
