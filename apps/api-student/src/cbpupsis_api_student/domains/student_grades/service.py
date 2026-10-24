"""Student read facade and bounded-concurrency in-memory PDF export."""

import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.student_grades.exports import transcript_pdf
from cbpupsis_shared.domains.grading.client import read_grades, read_transcript
from cbpupsis_shared.domains.grading.exceptions import GradeRecordUnavailableError

__all__ = ["export_transcript", "read_grades", "read_transcript"]

_PDF_SLOTS = asyncio.Semaphore(2)


def _render_finished(task: asyncio.Task) -> None:
    _PDF_SLOTS.release()
    if not task.cancelled():
        # Observe failures after a request disconnects.
        task.exception()


async def export_transcript(db: AsyncSession, *, student_id: uuid.UUID) -> bytes:
    acquired = False
    try:
        async with asyncio.timeout(15):
            record = await read_transcript(db, student_id=student_id, complete=True)
            # Release the read connection before waiting or rendering.
            await db.rollback()
            await _PDF_SLOTS.acquire()
            acquired = True
            job = asyncio.create_task(asyncio.to_thread(transcript_pdf, record))
            # A timeout cannot stop its thread. Keep the slot until
            # rendering finishes to avoid admitting unlimited work.
            job.add_done_callback(_render_finished)
            acquired = False
            try:
                return await asyncio.shield(job)
            except Exception:
                raise GradeRecordUnavailableError from None
    except (TimeoutError, ValueError) as exc:
        raise GradeRecordUnavailableError from exc
    finally:
        if acquired:
            _PDF_SLOTS.release()
