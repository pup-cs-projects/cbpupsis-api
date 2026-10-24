"""Private student records with a self-only identified grades read."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.student_grades import service
from cbpupsis_api_student.domains.student_grades.schemas import (
    GradesRead,
    TranscriptRead,
)
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.constants import ROLE_STUDENT
from cbpupsis_shared.domains.auth.dependencies import CurrentUser, get_current_user
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.grading.constants import READ_GRADE, READ_TRANSCRIPT
from cbpupsis_shared.domains.iam.dependencies import require_permission


async def private_record(request: Request):
    request.state.private_response = True


async def student_record_user(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if user.role != ROLE_STUDENT:
        raise ResourceNotFoundError
    return user


router = APIRouter(
    dependencies=[Depends(private_record), Depends(student_record_user)],
    responses={
        401: {
            "description": "Missing or invalid access token, expired/revoked session, "
            "or inactive account. A live authenticated session is required."
        },
        403: {
            "description": "Missing the required read permission, or a temporary "
            "password must be changed (PASSWORD_CHANGE_REQUIRED)."
        },
        404: {
            "description": "RESOURCE_NOT_FOUND: the session is not a student "
            "session, no student record is available, or the supplied student "
            "number is not the signed-in student's own number."
        },
        503: {
            "description": "GRADE_RECORD_UNAVAILABLE: stored academic data cannot "
            "be safely read. No partial or fabricated record is returned."
        },
    },
)
Student = Annotated[CurrentUser, Depends(student_record_user)]
Database = Annotated[AsyncSession, Depends(get_db)]
Year = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=20,
        description="Academic-year label exactly as stored. Supply together with "
        "semester, or omit both to select the latest term with posted marks.",
        examples=["2025-2026"],
    ),
]
Semester = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=30,
        description="Semester label exactly as stored, including letter case. "
        "Required when year is supplied; blank or unpaired filters return 422.",
        examples=["first"],
    ),
]
grade_permissions = [Depends(require_permission(READ_GRADE))]
transcript_permissions = [Depends(require_permission(READ_TRANSCRIPT))]


@router.get(
    "/students/me/grades", response_model=GradesRead, dependencies=grade_permissions
)
async def read_grades(
    user: Student,
    db: Database,
    year: Year = None,
    semester: Semester = None,
) -> GradesRead:
    """Read your finalized grades for one academic term (US-STU-004).

    Requires a live **student Bearer session** and the **ReadGrade**
    permission. `me` is resolved from the access token; no student number
    is needed. Other roles receive `404 RESOURCE_NOT_FOUND`.

    Omit both filters for the latest term containing posted marks, not
    necessarily the current academic term. To select history, supply
    `year` and `semester` together, for example `year=2025-2026` and
    `semester=first`. Labels must match the stored term exactly; blank
    or unpaired filters return **422**.

    Only finalized, locked, non-draft marks are visible. Each course has
    its code, title, units, final mark, status, posted date and any stored
    incomplete deadline. Decimal values are JSON **strings**. GWA is
    unit-weighted, excludes NSTP and nonnumeric marks, includes numeric
    failures and rounds half-up to two places. No eligible numeric units
    means `gwa: null`.

    No posted marks returns **200** with an explanatory empty state.
    Standing is read from a stored evaluation, never calculated here;
    missing evaluation leaves standing and `unitReduction` null. This
    read does not submit/correct marks or convert expired incompletes.
    Responses use `Cache-Control: private, no-store`.
    """
    return await service.read_grades(
        db, student_id=user.id, year=year, semester=semester
    )


@router.get(
    "/students/{student_number}/grades",
    response_model=GradesRead,
    dependencies=grade_permissions,
)
async def read_identified_grades(
    student_number: Annotated[
        str,
        Path(
            description="The signed-in student's own institutional student "
            "number, not a user UUID. Another student's number and an unknown "
            "number both return 404 RESOURCE_NOT_FOUND.",
            examples=["2021-00901-MN-0"],
        ),
    ],
    user: Student,
    db: Database,
    year: Year = None,
    semester: Semester = None,
) -> GradesRead:
    """Read your grades using an explicit student number (US-STU-004).

    Requires a live **student Bearer session** and **ReadGrade**. The
    path parameter must match the signed-in student's number exactly.
    This is **not** a staff lookup or a way to read another student's
    grades: foreign and unknown numbers both return
    `404 RESOURCE_NOT_FOUND` without confirming whether they exist.

    In Scalar, enter your own student number in the path field, for
    example `2021-00901-MN-0`, and use that same student's Bearer token.
    Prefer `/api/v1/students/me/grades` for normal frontend self-service.

    Term selection, calculation rules, empty states and response fields
    are identical to the `me` endpoint. Omit both term filters for the
    latest posted term, or supply `year` and `semester` together; blank
    or unpaired filters return **422**. Only finalized, non-draft marks
    are returned. Responses use `Cache-Control: private, no-store`.
    """
    return await service.read_grades(
        db,
        student_id=user.id,
        student_number=student_number,
        year=year,
        semester=semester,
    )


@router.get(
    "/students/me/transcript",
    response_model=TranscriptRead,
    dependencies=transcript_permissions,
)
async def read_transcript(
    user: Student,
    db: Database,
    page: Annotated[
        int,
        Query(
            ge=1,
            description="One-based page of academic terms, newest first. A page "
            "beyond the last returns empty terms while preserving overall totals.",
        ),
    ] = 1,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=100,
            description="Maximum term blocks per page, not courses. Defaults "
            "to 20 and accepts 1 through 100; never changes cumulative totals.",
        ),
    ] = 20,
) -> TranscriptRead:
    """Read your academic history, paginated by term (US-STU-004).

    Requires a live **student Bearer session** and **ReadTranscript**.
    Terms containing finalized, non-draft marks are ordered newest first.
    Pagination counts **terms**, not courses: `page` starts at 1 and
    `limit` defaults to 20 with a maximum of 100. Invalid values return
    **422**. `total` counts all available terms; `nextPage` is null when
    no later page exists.

    All eligible attempts, including repeated courses and numeric
    failures, contribute to GWA. NSTP and nonnumeric marks do not.
    Cumulative GWA uses completed terms: their end date must be before
    today's Asia/Manila date. Ongoing terms remain visible but do not
    contribute to cumulative GWA. Earned credits include posted passes,
    including ongoing terms, and count each catalog course only once.

    Top-level totals cover the **whole record**, independently of page
    and limit. Each term's running totals cover history through that
    term. Decimal values are strings; averages round half-up to two
    places and are null when there are no eligible numeric units.
    No posted marks returns **200** with empty terms and an explanation.

    Use `/api/v1/students/me/transcript/export` for a complete printable
    PDF, not just the current page. Responses are private and no-store.
    """
    return await service.read_transcript(db, student_id=user.id, page=page, limit=limit)


@router.get(
    "/students/me/transcript/export",
    response_class=Response,
    dependencies=transcript_permissions,
    responses={
        200: {
            "description": "Complete unofficial student transcript as a private "
            "PDF attachment, including an explanatory empty transcript if no "
            "finalized marks are available.",
            "content": {"application/pdf": {}},
        },
        503: {
            "description": "GRADE_RECORD_UNAVAILABLE: academic data is "
            "inconsistent, or PDF rendering failed or exceeded its time limit."
        },
    },
)
async def export_transcript(user: Student, db: Database) -> Response:
    """Download your complete unofficial transcript PDF (US-STU-004).

    Requires a live **student Bearer session** and **ReadTranscript**.
    The PDF includes the university header, your identity, all posted
    term tables, GWA/earned-unit totals and page numbers. It is labeled
    **Unofficial Transcript - Student Copy**, not an official registrar
    certification. Table headings repeat when records span pages.

    Export always includes the **complete record**, regardless of JSON
    pagination. This endpoint has no page/limit parameters or term
    filters; supplying them does not narrow the PDF. No posted marks
    still returns a valid PDF with an explanatory empty state.

    Success returns **200 application/pdf** with attachment filename
    `unofficial-transcript.pdf`, `Cache-Control: private, no-store`, and
    `X-Content-Type-Options: nosniff`. Use Scalar's response preview or
    download control; opening the URL in a new unauthenticated browser
    tab returns **401** because the Bearer token is not sent there.

    Generation is in memory; no transcript file is stored by the API.
    Rendering failure, timeout or inconsistent source data returns
    `503 GRADE_RECORD_UNAVAILABLE`, never a partial academic record.
    """
    content = await service.export_transcript(db, student_id=user.id)
    return Response(
        content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="unofficial-transcript.pdf"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
