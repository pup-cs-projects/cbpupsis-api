"""Student schedule HTTP surface; exported documents remain private."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response
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
Year = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=20,
        description=(
            "Stored academic-year string, not a calendar-year integer. Supply with "
            "semester, or omit both to use the current academic term."
        ),
        examples=["2026-2027"],
    ),
]
Semester = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=30,
        description=(
            "Semester identifier as stored for the academic term. Supply with year, "
            "or omit both for the current term; a single selector is rejected."
        ),
        examples=["first"],
    ),
]
Filter = Annotated[
    str,
    Query(
        max_length=200,
        description=(
            "Case-insensitive substring search across course code, course title "
            "and faculty name. Blank or whitespace-only values restore all courses. "
            "Only returned rows and totals are filtered; enrollment is never changed."
        ),
        examples=["CS001", "Algorithms", "Synthetic Faculty"],
    ),
]
Student = Annotated[CurrentUser, Depends(require_student_session)]
Database = Annotated[AsyncSession, Depends(get_db)]

READ_DESCRIPTION = """
Read the signed-in student's officially enrolled courses, meeting times and weekly
contact-hour totals (US-STU-003).

**Authentication:** a live student bearer session and `ReadSchedule` permission
are required. Only the caller's records are accessible; this is not a staff or
read-all endpoint. Downloading a Certificate of Registration is not required.

**Term selection:** omit both `year` and `semester` for the current academic term,
or supply both, for example `year=2026-2027&semester=first`. Historical configured
terms are supported. Supplying just one selector or blank selectors returns 422.

**Filtering:** `q` searches course code, title and faculty name case-insensitively.
For example, `q=CS001` narrows the rows and their totals. Clear `q` to restore the
full list; searching does not modify enrollment.

**Response:** a direct snake_case JSON object, without a `status`/`data` wrapper.
Each course includes its section, rooms, faculty, credit units, enrollment status,
separate lecture/lab hours and every meeting. Contact hours are weekly durations
calculated from meetings, not credit conversions or term-wide totals. Decimal
quantities serialize as strings. Totals sum only the returned courses.

**Empty states still return 200:** `notEnrolled` means no official enrollment for
the selected term; `noMatches` means the filter matched no enrolled courses;
`termUnavailable` means the requested/current term configuration is unavailable.
These responses include an explanatory `message`, empty `courses` and zero totals.
Nonempty results have `state=ready`. Pending and dropped courses are excluded;
committed enrollment changes are reflected on subsequent reads.

Responses are private and use `Cache-Control: private, no-store`.
"""

ERROR_RESPONSES = {
    401: {
        "description": "Missing, invalid, expired or inactive student bearer session."
    },
    403: {
        "description": "The caller is not a student or lacks ReadSchedule permission."
    },
    404: {
        "description": (
            "RESOURCE_NOT_FOUND: no accessible student identity. Foreign and "
            "nonexistent student numbers receive indistinguishable ownership errors."
        )
    },
    422: {
        "description": (
            "Invalid query parameters: supply year and semester together with "
            "nonblank values, respect field lengths and use a supported export format."
        )
    },
    503: {
        "description": "SERVICE_UNAVAILABLE: schedule data or generation unavailable.",
        "headers": {
            "Retry-After": {
                "description": "Wait 30 seconds before retrying.",
                "schema": {"type": "integer", "example": 30},
            }
        },
    },
}


@router.get(
    "/students/me/schedule",
    response_model=ScheduleRead,
    summary="Read My Course Schedule",
    description=READ_DESCRIPTION,
    responses={
        **ERROR_RESPONSES,
        200: {
            "description": "Caller-owned courses and totals, or explained empty state."
        },
    },
)
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
    "/students/me/schedule/export",
    summary="Export My Course Schedule",
    description="""
Download the signed-in student's complete officially enrolled schedule as a PDF
or iCalendar attachment (US-STU-003). Requires a live student bearer session and
`ReadSchedule` permission, just like the schedule read endpoint.

**Choose a format:** `format=pdf` produces a university-headed printable document
with student identity, term, every course/meeting, credit units, separate weekly
lecture/lab hours and totals. `format=ics` produces an iCalendar file with one
recurring event series per meeting slot, preserving separate lecture/lab times.

**Select a term:** omit both `year` and `semester` for the current term, or supply
both, for example `year=2026-2027&semester=first`. Historical configured terms are
supported. The export always includes the complete selected term; the read
endpoint's `q` filter does not restrict downloads and is not an export parameter.
No enrollment produces an empty schedule document/calendar. Missing term
configuration produces an explanatory PDF or an empty calendar, not an ownership
404.

**Calendar behavior:** meeting times use Asia/Manila, weekly recurrences end at
the term boundary, and configured academic holidays are excluded. Stable opaque
UIDs and durable SEQUENCE revisions support unchanged re-exports, updates and
cancellation records for removed meetings. Import/re-import behavior depends on
the calendar client: downloading a file is not Google authorization or automatic
synchronization.

**Download and failures:** 200 returns the file bytes, not JSON, with attachment
filename `course-schedule.pdf` or `course-schedule.ics`. The entire file is
buffered before responding. Generation has a ten-second deadline; failures
return 503 `SERVICE_UNAVAILABLE` with `Retry-After: 30`, never a partial attachment.
Responses are private and use `Cache-Control: private, no-store`.
""",
    response_class=Response,
    responses={
        **ERROR_RESPONSES,
        200: {
            "description": "Complete schedule file attachment in the requested format.",
            "content": {
                media_type: {"schema": {"type": "string", "format": "binary"}}
                for media_type in ("application/pdf", "text/calendar")
            },
            "headers": {
                "Content-Disposition": {
                    "description": (
                        "Attachment named course-schedule.pdf or course-schedule.ics."
                    ),
                    "schema": {"type": "string"},
                },
                "Cache-Control": {
                    "description": "Private document; must not be stored in caches.",
                    "schema": {"type": "string", "example": "private, no-store"},
                },
            },
        },
    },
)
async def export_schedule(
    user: Student,
    db: Database,
    format: Annotated[
        Literal["pdf", "ics"],
        Query(
            description=(
                "Required download format: pdf for printing, ics for calendar import."
            ),
            examples=["pdf", "ics"],
        ),
    ],
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


@router.get(
    "/students/{student_number}/schedule",
    response_model=ScheduleRead,
    summary="Read My Schedule by Student Number",
    description=(
        READ_DESCRIPTION
        + """

**Explicit ownership check:** `student_number` must exactly match the signed-in
student's institutional number. A different student's number and a nonexistent
number both return the same 404 `RESOURCE_NOT_FOUND`; this endpoint cannot look
up other students. Prefer `/api/v1/students/me/schedule` when the client does not
need to send an explicit identifier. Term selectors, filtering and totals behave
identically to that self route.
"""
    ),
    responses={
        **ERROR_RESPONSES,
        200: {
            "description": "The caller's schedule after verifying the supplied number."
        },
    },
)
async def read_identified_schedule(
    student_number: Annotated[
        str,
        Path(
            description=(
                "Your own institutional student number; must match the bearer session."
            ),
            examples=["2021-00123-MN-0"],
        ),
    ],
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
