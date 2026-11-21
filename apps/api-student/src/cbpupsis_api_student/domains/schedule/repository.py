"""Export state persistence. The service owns the surrounding transaction."""

import uuid

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.schedule import ScheduleExportState


async def begin_snapshot(db: AsyncSession) -> None:
    """Pin multi-domain reads to a coherent PostgreSQL snapshot.

    Called only at the start of a new transaction; SQLite's test connection
    uses its existing transaction behavior.

    SQL::

        -- Subsequent SELECT/UPDATE statements share a transaction snapshot.
        SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;
    """
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))


async def lock_export_state(
    db: AsyncSession, student_id: uuid.UUID, term_id: uuid.UUID
) -> ScheduleExportState:
    """Create the stream if absent, then serialize its exports in PostgreSQL.

    The conflicting INSERT also waits for the first-ever export's transaction;
    locking only a SELECT would leave that creation race unprotected.

    SQL::

        INSERT INTO schedule_export_states (student_id, academic_term_id)
        VALUES (:student_id, :term_id) ON CONFLICT DO NOTHING;
        SELECT * FROM schedule_export_states
        WHERE student_id = :student_id AND academic_term_id = :term_id FOR UPDATE;
    """
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    await db.execute(
        insert(ScheduleExportState)
        .values(student_id=student_id, academic_term_id=term_id)
        .on_conflict_do_nothing()
    )
    return await db.scalar(
        select(ScheduleExportState)
        .where(
            ScheduleExportState.student_id == student_id,
            ScheduleExportState.academic_term_id == term_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
