"""Data access for the sections domain. CRUD only: no rules, no authorization."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import time

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.sections import CourseSection, SectionSchedule


async def ids_for_courses(db: AsyncSession, course_ids):
    """Resolve already-authorized courses to their section identifiers.

    SQL::

        SELECT id FROM course_sections WHERE course_id IN (:course_ids);
    """
    if not course_ids:
        return []
    return list(
        await db.scalars(
            select(CourseSection.id).where(CourseSection.course_id.in_(course_ids))
        )
    )


async def schedule_sections(db: AsyncSession, ids: list[uuid.UUID]):
    """Read section metadata and meeting slots in two bounded queries.

    SQL::

        SELECT * FROM course_sections WHERE id IN (:ids);
        SELECT * FROM section_schedules WHERE section_id IN (:ids)
        ORDER BY day_of_week, start_time, id LIMIT 1001;
    """
    if not ids:
        return [], []
    sections = await db.scalars(select(CourseSection).where(CourseSection.id.in_(ids)))
    meetings = await db.scalars(
        select(SectionSchedule)
        .where(SectionSchedule.section_id.in_(ids))
        .order_by(
            SectionSchedule.day_of_week, SectionSchedule.start_time, SectionSchedule.id
        )
        .limit(1001)
    )
    return list(sections), list(meetings)


async def list_available_sections(
    db: AsyncSession,
    *,
    academic_term_id: uuid.UUID,
    course_ids: Collection[uuid.UUID],
):
    """Read open offerings and their meetings for a term and course set.

    SQL::

        SELECT * FROM course_sections
        WHERE academic_term_id = :academic_term_id
          AND course_id IN (:course_ids)
          AND lower(status) = 'open'
        ORDER BY course_id, section_name, id LIMIT 1001;

        SELECT * FROM section_schedules
        WHERE section_id IN (:section_ids)
        ORDER BY section_id, day_of_week, start_time, id LIMIT 1001;
    """
    if not course_ids:
        return [], []
    sections = list(
        await db.scalars(
            select(CourseSection)
            .where(
                CourseSection.academic_term_id == academic_term_id,
                CourseSection.course_id.in_(course_ids),
                func.lower(CourseSection.status) == "open",
            )
            .order_by(
                CourseSection.course_id,
                CourseSection.section_name,
                CourseSection.id,
            )
            .limit(1001)
        )
    )
    if not sections:
        return [], []
    meetings = await db.scalars(
        select(SectionSchedule)
        .where(SectionSchedule.section_id.in_([row.id for row in sections]))
        .order_by(
            SectionSchedule.section_id,
            SectionSchedule.day_of_week,
            SectionSchedule.start_time,
            SectionSchedule.id,
        )
        .limit(1001)
    )
    return sections, list(meetings)


async def mark_seats_taken(db: AsyncSession, ids: Collection[uuid.UUID]) -> None:
    """Write a new version of sections whose seats were just allocated.

    A REPEATABLE READ writer that locks one of these rows afterwards then fails
    with a serialization error and retries, instead of counting seats from a
    snapshot that predates this allocation. The caller holds the row locks.

    SQL::

        UPDATE course_sections SET capacity = capacity WHERE id IN (:ids);
    """
    await db.execute(
        update(CourseSection)
        .where(CourseSection.id.in_(list(ids)))
        .values(capacity=CourseSection.capacity)
        .execution_options(synchronize_session=False)
    )


async def lock_sections_for_enrollment(db: AsyncSession, ids: Collection[uuid.UUID]):
    """Lock selected section rows in stable order and read their meetings.

    Stable ordering avoids deadlocks when concurrent submissions contain the
    same sections in a different request order. PostgreSQL holds these row
    locks through the caller's commit or rollback.

    SQL::

        SELECT * FROM course_sections
        WHERE id IN (:ids)
        ORDER BY id
        FOR UPDATE;

        SELECT * FROM section_schedules
        WHERE section_id IN (:ids)
        ORDER BY section_id, day_of_week, start_time, id LIMIT 1001;
    """
    if not ids:
        return [], []
    ordered_ids = sorted(ids, key=str)
    section_rows = await db.scalars(
        select(CourseSection)
        .where(CourseSection.id.in_(ordered_ids))
        .order_by(CourseSection.id)
        .with_for_update()
    )
    sections = list(section_rows)
    meetings = await db.scalars(
        select(SectionSchedule)
        .where(SectionSchedule.section_id.in_(ordered_ids))
        .order_by(
            SectionSchedule.section_id,
            SectionSchedule.day_of_week,
            SectionSchedule.start_time,
            SectionSchedule.id,
        )
        .limit(1001)
    )
    return sections, list(meetings)


async def list_record_sections(db: AsyncSession, ids: Collection[uuid.UUID]):
    """Read section-to-course/term identifiers in one batch.

    SQL::

        SELECT * FROM course_sections WHERE id IN (:ids);
    """
    if not ids:
        return []
    return list(
        await db.scalars(select(CourseSection).where(CourseSection.id.in_(ids)))
    )


async def get_course_section(
    db: AsyncSession, section_id: uuid.UUID
) -> CourseSection | None:
    """Return one section by id, or ``None``.

    SQL::

        SELECT * FROM course_sections WHERE id = :section_id
    """
    return await db.get(CourseSection, section_id)


async def list_course_sections_for_faculty(
    db: AsyncSession, faculty_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[CourseSection], int]:
    """Return one page of a faculty member's sections and the total count.

    SQL::

        SELECT count(*) FROM course_sections WHERE faculty_id = :faculty_id;

        SELECT * FROM course_sections WHERE faculty_id = :faculty_id
        ORDER BY section_name, id
        LIMIT :limit OFFSET :offset
    """
    total = await db.scalar(
        select(func.count())
        .select_from(CourseSection)
        .where(CourseSection.faculty_id == faculty_id)
    )
    result = await db.scalars(
        select(CourseSection)
        .where(CourseSection.faculty_id == faculty_id)
        .order_by(CourseSection.section_name, CourseSection.id)
        .limit(limit)
        .offset(offset)
    )
    return list(result.all()), total or 0


def add_course_section(
    db: AsyncSession,
    *,
    academic_term_id: uuid.UUID,
    course_id: uuid.UUID,
    faculty_id: uuid.UUID | None,
    section_name: str,
    capacity: int,
    status: str = "open",
) -> CourseSection:
    """Stage a new course section row.

    SQL::

        INSERT INTO course_sections (id, academic_term_id, course_id,
                                     faculty_id, section_name, capacity, status)
        VALUES (:id, :academic_term_id, :course_id, :faculty_id,
                :section_name, :capacity, :status)
    """
    section = CourseSection(
        academic_term_id=academic_term_id,
        course_id=course_id,
        faculty_id=faculty_id,
        section_name=section_name,
        capacity=capacity,
        status=status,
    )
    db.add(section)
    return section


def update_course_section(section: CourseSection, **fields: object) -> CourseSection:
    """Update fields on a loaded course section row.

    SQL::

        UPDATE course_sections SET capacity = :capacity, ... WHERE id = :id
    """
    for key, value in fields.items():
        setattr(section, key, value)
    return section


async def list_section_schedules(
    db: AsyncSession, section_id: uuid.UUID
) -> list[SectionSchedule]:
    """List schedule slots for a course section.

    SQL::

        SELECT * FROM section_schedules
        WHERE section_id = :section_id
        ORDER BY day_of_week ASC, start_time ASC, id ASC
    """
    return list(
        await db.scalars(
            select(SectionSchedule)
            .where(SectionSchedule.section_id == section_id)
            .order_by(
                SectionSchedule.day_of_week.asc(),
                SectionSchedule.start_time.asc(),
                SectionSchedule.id.asc(),
            )
        )
    )


async def replace_section_schedules(
    db: AsyncSession,
    *,
    section_id: uuid.UUID,
    meetings: list[tuple[int, time, time, str | None, str]],
) -> list[SectionSchedule]:
    """Replace schedule meeting slots for ``section_id``.

    SQL::

        DELETE FROM section_schedules WHERE section_id = :section_id;
        INSERT INTO section_schedules (id, section_id, day_of_week, start_time,
                                       end_time, room, schedule_type)
        VALUES (:id, :section_id, :day_of_week, :start_time, :end_time,
                :room, :schedule_type);
    """
    await db.execute(
        delete(SectionSchedule).where(SectionSchedule.section_id == section_id)
    )
    rows: list[SectionSchedule] = []
    for day_of_week, start_time, end_time, room, schedule_type in meetings:
        row = SectionSchedule(
            section_id=section_id,
            day_of_week=day_of_week,
            start_time=start_time,
            end_time=end_time,
            room=room,
            schedule_type=schedule_type,
        )
        db.add(row)
        rows.append(row)
    return rows


async def update_section_schedules_room(
    db: AsyncSession, *, section_id: uuid.UUID, room: str | None
) -> None:
    """Update the room across existing schedule slots for ``section_id``.

    SQL::

        SELECT * FROM section_schedules WHERE section_id = :section_id;
        UPDATE section_schedules SET room = :room WHERE section_id = :section_id;
    """
    schedules = await list_section_schedules(db, section_id)
    for sched in schedules:
        sched.room = room
