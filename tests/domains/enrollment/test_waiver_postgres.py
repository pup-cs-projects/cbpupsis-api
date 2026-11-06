"""One disposable PostgreSQL transition-lock check, not a seat-contention campaign."""

import asyncio
import os
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cbpupsis_core.exceptions import ConflictError
from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.curriculum import Course, Program
from cbpupsis_database.models.enrollment import CourseWaiver
from cbpupsis_database.models.outbox import OutboxMessage
from cbpupsis_database.models.sections import CourseSection
from cbpupsis_database.models.users import StudentProfile, User
from cbpupsis_shared.domains.enrollment import repository, waiver
from cbpupsis_shared.domains.enrollment.schemas import WaiverDecision, WaiverReviewer


@pytest.mark.skipif(
    not os.environ.get("ISSUE99_POSTGRES_URL"),
    reason="Requires an explicitly designated disposable migrated PostgreSQL DB",
)
async def test_concurrent_replayed_decision_has_one_success_one_audit_one_notice():
    engine = create_async_engine(os.environ["ISSUE99_POSTGRES_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    student, chair, term_id, course_id, section_id, waiver_id = [
        uuid.uuid4() for _ in range(6)
    ]
    label = uuid.uuid4().hex[:16]
    task = None
    try:
        async with sessions() as db:
            db.add_all(
                [
                    User(id=uid, email=f"{uid}@example.com", password_hash="!fixture")
                    for uid in (student, chair)
                ]
            )
            program = Program(
                id=uuid.uuid4(),
                code=label,
                name="Synthetic",
                department=label,
                college=label,
                total_units_required=120,
            )
            now = datetime.now(UTC)
            db.add_all(
                [
                    program,
                    Course(
                        id=course_id,
                        course_code=label,
                        title="Synthetic",
                        is_nstp=False,
                    ),
                    AcademicTerm(
                        id=term_id,
                        academic_year=label,
                        semester="FIRST",
                        term_type="REGULAR",
                        start_date=date(2026, 1, 1),
                        end_date=date(2026, 5, 1),
                        regular_enrollment_start=now,
                        regular_enrollment_end=now,
                        add_drop_deadline=now,
                        grade_submission_deadline=now,
                    ),
                ]
            )
            await db.flush()
            db.add_all(
                [
                    StudentProfile(
                        user_id=student,
                        student_number=label,
                        program_id=program.id,
                        year_level=1,
                    ),
                    CourseSection(
                        id=section_id,
                        course_id=course_id,
                        academic_term_id=term_id,
                        section_name="A",
                        capacity=30,
                    ),
                ]
            )
            await db.flush()
            db.add(
                CourseWaiver(
                    id=waiver_id,
                    student_id=student,
                    section_id=section_id,
                    prerequisite_course_code="EXPLICIT",
                    condition="GRADUATING",
                    justification="Synthetic",
                    state="PENDING_CHAIR",
                )
            )
            await db.commit()
        reviewer = WaiverReviewer(id=chair, position="chairperson", department=label)
        async with sessions() as first, sessions() as second:
            await repository.lock_scoped_waiver(first, waiver_id, [student])
            task = asyncio.create_task(
                waiver.decide_waiver(
                    second,
                    reviewer=reviewer,
                    waiver_id=waiver_id,
                    data=WaiverDecision(decision="APPROVE"),
                )
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), 0.15)
            result = await waiver.decide_waiver(
                first,
                reviewer=reviewer,
                waiver_id=waiver_id,
                data=WaiverDecision(decision="APPROVE"),
            )
            assert result.state == "PENDING_DEAN"
            with pytest.raises(ConflictError) as error:
                await asyncio.wait_for(task, 5)
            assert error.value.code == "ENROLL_WAIVER_STEP_MISMATCH"
        async with sessions() as verify:
            assert (await verify.get(CourseWaiver, waiver_id)).state == "PENDING_DEAN"
            assert (
                await verify.scalar(
                    select(func.count())
                    .select_from(AuditEntry)
                    .where(AuditEntry.target_id == str(waiver_id))
                )
                == 1
            )
            assert (
                await verify.scalar(
                    select(func.count())
                    .select_from(OutboxMessage)
                    .where(
                        OutboxMessage.payload["waiverRequestId"].as_string()
                        == str(waiver_id)
                    )
                )
                == 1
            )
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # Audit rows are append-only. The owner discards the whole disposable
        # container after this check, never deletes ledger rows for cleanup.
        await engine.dispose()
