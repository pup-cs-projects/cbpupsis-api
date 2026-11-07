"""Guarded catalogue writes reject cycles before persistence."""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cbpupsis_core.exceptions import NotFoundError, ValidationError
from cbpupsis_database.models.curriculum import (
    Course,
    CoursePrerequisite,
    Curriculum,
    CurriculumCourse,
    Program,
)
from cbpupsis_shared.domains.curriculum import client, repository


@pytest.fixture
async def courses(db):
    rows = [Course(course_code=code, title=code) for code in ("A", "B", "C", "D")]
    db.add_all(rows)
    await db.commit()
    return rows


async def test_ac_enrl_002_10_catalogue_cycle_refused_before_persistence(db, courses):
    a, b, c, _ = courses
    await client.stage_replace_prerequisites(
        db, course_id=a.id, prerequisite_course_ids=[b.id]
    )
    await client.stage_replace_prerequisites(
        db, course_id=b.id, prerequisite_course_ids=[c.id]
    )
    await db.commit()
    with pytest.raises(ValidationError, match="cycle") as error:
        await client.stage_replace_prerequisites(
            db, course_id=c.id, prerequisite_course_ids=[a.id]
        )
    assert error.value.code == "CURRICULUM_PREREQUISITE_CYCLE"
    assert await repository.prerequisite_graph(db) == {(a.id, b.id), (b.id, c.id)}


async def test_self_dependency_and_missing_course_are_refused(db, courses):
    a, *_ = courses
    with pytest.raises(ValidationError, match="self-dependency"):
        await client.stage_replace_prerequisites(
            db, course_id=a.id, prerequisite_course_ids=[a.id]
        )
    with pytest.raises(NotFoundError):
        await client.stage_replace_prerequisites(
            db, course_id=a.id, prerequisite_course_ids=[uuid.uuid4()]
        )
    assert await repository.prerequisite_graph(db) == set()


async def test_valid_replacement_preserves_retained_metadata_and_rolls_back(
    db, courses
):
    a, b, c, d = courses
    a_id, b_id, c_id = a.id, b.id, c.id
    db.add(
        CoursePrerequisite(
            course_id=a.id,
            prerequisite_course_id=b.id,
            min_grade="2.00",
            is_corequisite=True,
        )
    )
    await db.commit()
    await client.stage_replace_prerequisites(
        db, course_id=a.id, prerequisite_course_ids=[b.id, c.id, c.id]
    )
    await db.commit()
    retained = await db.scalar(
        select(CoursePrerequisite).where(
            CoursePrerequisite.course_id == a.id,
            CoursePrerequisite.prerequisite_course_id == b.id,
        )
    )
    assert retained.min_grade == "2.00"
    assert retained.is_corequisite is True
    await client.stage_replace_prerequisites(
        db, course_id=a.id, prerequisite_course_ids=[d.id]
    )
    await db.rollback()
    assert await repository.prerequisite_graph(db) == {(a_id, b_id), (a_id, c_id)}


async def test_postgresql_graph_lock_precedes_graph_read(monkeypatch):
    """Unit evidence for lock order; real PostgreSQL contention is separate."""
    db = MagicMock()
    db.get_bind.return_value.dialect.name = "postgresql"
    connection = AsyncMock()
    connection.get_isolation_level.return_value = "READ COMMITTED"
    db.connection = AsyncMock(return_value=connection)
    db.execute = AsyncMock()
    await repository.lock_prerequisite_graph(db)
    assert str(db.execute.call_args.args[0]) == "SELECT pg_advisory_xact_lock(99002001)"
    connection.get_isolation_level.return_value = "REPEATABLE READ"
    with pytest.raises(RuntimeError, match="READ COMMITTED"):
        await repository.lock_prerequisite_graph(db)


async def test_academic_catalog_batches_curriculum_and_prerequisite_courses(
    db, courses
):
    a, b, c, d = courses
    a.pe_sequence = 1
    a.is_nstp = False
    b.is_nstp = True
    c.is_nstp = False
    program = Program(
        code="CATALOG",
        name="Catalogue test",
        department="Mathematics",
        college="Sciences",
        total_units_required=Decimal("100"),
    )
    db.add(program)
    await db.flush()
    curriculum = Curriculum(program_id=program.id, curriculum_year="2026")
    db.add(curriculum)
    await db.flush()
    db.add_all(
        [
            CurriculumCourse(
                curriculum_id=curriculum.id,
                course_id=a.id,
                year_level=1,
                semester="First",
            ),
            CurriculumCourse(
                curriculum_id=curriculum.id,
                course_id=b.id,
                year_level=3,
                semester="First",
            ),
            CoursePrerequisite(course_id=c.id, prerequisite_course_id=d.id),
        ]
    )
    await db.commit()
    catalog = await client.academic_catalog(db, curriculum.id, [c.id])
    assert {course.id for course in catalog.courses} == {a.id, b.id, c.id, d.id}
    assert catalog.placements == {a.id: 1, b.id: 3}
    assert [
        (edge.course_id, edge.prerequisite_course_id) for edge in catalog.prerequisites
    ] == [(c.id, d.id)]
    assert {course.id: course.is_nstp for course in catalog.courses} == {
        a.id: False,
        b.id: True,
        c.id: False,
        d.id: None,
    }


async def test_program_scope_uses_explicit_membership_and_fails_closed(db):
    programs = [
        Program(
            code=code,
            name=code,
            department=department,
            college=college,
            total_units_required=Decimal("100"),
        )
        for code, department, college in [
            ("MATH", "Mathematics", "Sciences"),
            ("CS", "Computer Science", "Sciences"),
            ("LAW", "Legal Studies", "Law"),
            ("UNKNOWN", "Mathematics", None),
        ]
    ]
    db.add_all(programs)
    await db.commit()
    assert await client.program_ids_in_scope(db, department="Mathematics") == [
        programs[0].id
    ]
    assert set(await client.program_ids_in_scope(db, college="Sciences")) == {
        programs[0].id,
        programs[1].id,
    }
    assert await client.program_ids_in_scope(db, college="Mathematics") == []
    assert await client.program_ids_in_scope(db) == []
    assert await client.program_ids_in_scope(db, department="") == []


@pytest.mark.skipif(
    not os.environ.get("ISSUE99_POSTGRES_URL"),
    reason="Requires an explicitly designated migrated disposable PostgreSQL database",
)
async def test_postgresql_concurrent_graph_writers_cannot_create_cycle():
    """Opt-in locking check against a migrated disposable PostgreSQL database."""
    url = make_url(os.environ["ISSUE99_POSTGRES_URL"]).set(
        drivername="postgresql+asyncpg"
    )
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ids = [uuid.uuid4(), uuid.uuid4()]
    task = None
    try:
        async with sessions() as setup:
            setup.add_all(
                Course(
                    id=course_id,
                    course_code=f"L{str(course_id)[:16]}",
                    title="Lock test",
                )
                for course_id in ids
            )
            await setup.commit()
        async with sessions() as first, sessions() as second:
            await client.stage_replace_prerequisites(
                first, course_id=ids[0], prerequisite_course_ids=[ids[1]]
            )
            task = asyncio.create_task(
                client.stage_replace_prerequisites(
                    second, course_id=ids[1], prerequisite_course_ids=[ids[0]]
                )
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.15)
            await first.commit()
            with pytest.raises(ValidationError, match="cycle"):
                await asyncio.wait_for(task, timeout=5)
            await second.rollback()
            assert not await second.scalar(
                select(CoursePrerequisite).where(CoursePrerequisite.course_id == ids[1])
            )
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with sessions() as cleanup:
            await cleanup.execute(delete(Course).where(Course.id.in_(ids)))
            await cleanup.commit()
        await engine.dispose()
