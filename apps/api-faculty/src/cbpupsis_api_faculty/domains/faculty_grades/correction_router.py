"""Faculty Correction of Entry intake and filer-only tracking."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_faculty.domains.faculty_auth.dependencies import (
    require_faculty_session,
)
from cbpupsis_api_faculty.domains.faculty_grades import service
from cbpupsis_core.middleware import client_ip_or_none
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.grading.constants import (
    CREATE_GRADE_CORRECTION,
    MAX_GRADING_SHEET_BYTES,
    READ_GRADE_CORRECTION,
)
from cbpupsis_shared.domains.grading.schemas import (
    CorrectionPage,
    CorrectionRead,
)
from cbpupsis_shared.domains.iam.dependencies import require_permission


def private_correction_request(request: Request) -> None:
    request.state.private_response = True
    request.state.redact_validation_errors = True
    request.state.safe_error_metadata = True


router = APIRouter(
    dependencies=[
        Depends(private_correction_request),
        Depends(require_faculty_session),
    ],
    responses={
        401: {"description": "AUTH_SESSION_EXPIRED or AUTH_MFA_REQUIRED."},
        403: {"description": "AUTH_INSUFFICIENT_ROLE or missing permission."},
        404: {"description": "RESOURCE_NOT_FOUND for a missing or unowned resource."},
    },
)


@router.post(
    "/sections/{section_id}/grade-corrections",
    status_code=201,
    response_model=CorrectionRead,
    operation_id="file_faculty_grade_correction",
    dependencies=[Depends(require_permission(CREATE_GRADE_CORRECTION))],
    responses={
        403: {"description": "CORRECTION_WINDOW_CLOSED with windowClosedAt."},
        413: {"description": "FILE_TOO_LARGE (maximum 10 MB)."},
        415: {
            "description": "UNSUPPORTED_MEDIA_TYPE; content must be PDF, JPEG or PNG."
        },
        422: {
            "description": "VALIDATION_ERROR for an invalid filing or nonfinal grade."
        },
        503: {
            "description": "GRADE_RECORD_UNAVAILABLE: missing calendar/catalog scope."
        },
    },
)
async def file_grade_correction(
    request: Request,
    response: Response,
    section_id: uuid.UUID,
    student_id: uuid.UUID = Form(...),
    proposed_mark: str = Form(...),
    reason: str = Form(...),
    grading_sheet: UploadFile = File(...),
    user: CurrentUser = Depends(require_faculty_session),
    db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        content = await grading_sheet.read(MAX_GRADING_SHEET_BYTES + 1)
        return await service.file_correction(
            db,
            faculty_id=user.id,
            section_id=section_id,
            student_id=student_id,
            proposed_mark=proposed_mark,
            reason=reason,
            content=content,
            client_ip=client_ip_or_none(request),
        )
    finally:
        await grading_sheet.close()


@router.get(
    "/grade-corrections",
    response_model=CorrectionPage,
    operation_id="list_faculty_grade_corrections",
    dependencies=[Depends(require_permission(READ_GRADE_CORRECTION))],
)
async def list_grade_corrections(
    response: Response,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: CurrentUser = Depends(require_faculty_session),
    db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = "private, no-store"
    return await service.list_corrections(
        db, faculty_id=user.id, limit=limit, offset=offset
    )


@router.get(
    "/grade-corrections/{correction_id}",
    response_model=CorrectionRead,
    operation_id="get_faculty_grade_correction",
    dependencies=[Depends(require_permission(READ_GRADE_CORRECTION))],
)
async def get_grade_correction(
    correction_id: uuid.UUID,
    response: Response,
    user: CurrentUser = Depends(require_faculty_session),
    db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = "private, no-store"
    return await service.read_correction(db, correction_id, faculty_id=user.id)
