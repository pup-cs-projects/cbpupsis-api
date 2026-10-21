"""Student schedule HTTP surface; exported documents remain private."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.schedule import service
from cbpupsis_api_student.domains.schedule.constants import READ_SCHEDULE
from cbpupsis_api_student.domains.schedule.schemas import ScheduleRead
from cbpupsis_api_student.domains.student_auth.dependencies import (
    require_student_session,
)
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.iam.dependencies import require_permission


async def private_response(request: Request) -> None:
    """Include authorization and validation errors in the no-store policy."""
    request.state.private_response = True


router = APIRouter(
    dependencies=[Depends(private_response), Depends(require_permission(READ_SCHEDULE))]
)
Year = Annotated[str | None, Query(min_length=1, max_length=20)]
Semester = Annotated[str | None, Query(min_length=1, max_length=30)]
Filter = Annotated[str, Query(max_length=200)]
Student = Annotated[CurrentUser, Depends(require_student_session)]
Database = Annotated[AsyncSession, Depends(get_db)]


@router.get("/student/schedule", response_model=ScheduleRead)
async def read_schedule(
    response: Response,
    user: Student,
    db: Database,
    year: Year = None,
    semester: Semester = None,
    q: Filter = "",
) -> ScheduleRead:
    response.headers["Cache-Control"] = "private, no-store"
    return await service.read_schedule(
        db, student_id=user.id, year=year, semester=semester, q=q
    )


@router.get(
    "/student/schedule/export",
    response_class=Response,
    responses={
        200: {"content": {"application/pdf": {}, "text/calendar": {}}},
        503: {"description": "SERVICE_UNAVAILABLE; retry without a partial file"},
    },
)
async def export_schedule(
    user: Student,
    db: Database,
    format: Literal["pdf", "ics"],
    year: Year = None,
    semester: Semester = None,
) -> Response:
    content = await service.export_schedule(
        db, student_id=user.id, format=format, year=year, semester=semester
    )
    return Response(
        content,
        media_type="application/pdf" if format == "pdf" else "text/calendar",
        headers={
            "Content-Disposition": f'attachment; filename="course-schedule.{format}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/students/{student_number}/schedule", response_model=ScheduleRead)
async def read_identified_schedule(
    student_number: str,
    response: Response,
    user: Student,
    db: Database,
    year: Year = None,
    semester: Semester = None,
    q: Filter = "",
) -> ScheduleRead:
    response.headers["Cache-Control"] = "private, no-store"
    return await service.read_schedule(
        db,
        student_id=user.id,
        student_number=student_number,
        year=year,
        semester=semester,
        q=q,
    )
