"""HTTP routing for faculty grade submission, mounted at /api/v1/faculty/sections.

Every endpoint requires an MFA-completed faculty session, the endpoint's grading
or section permission, and section ownership enforced in the service layer.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Body, Depends, Path, Request
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_faculty.domains.faculty_auth.dependencies import (
    require_faculty_session,
)
from cbpupsis_api_faculty.domains.faculty_grades import service
from cbpupsis_api_faculty.domains.faculty_grades.schemas import (
    FinalizeGradesRead,
    FinalizeGradesRequest,
    GradeDeadlineRead,
    SaveGradeDraftRequest,
    SectionGradesRead,
)
from cbpupsis_core.middleware import client_ip_or_none
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.grading.constants import SUBMIT_GRADE, UPDATE_GRADE
from cbpupsis_shared.domains.iam.dependencies import require_permission
from cbpupsis_shared.domains.sections.constants import READ_COURSE_SECTION

router = APIRouter(
    dependencies=[Depends(require_faculty_session)],
    responses={
        401: {
            "description": "Session expired (AUTH_SESSION_EXPIRED) or MFA required "
            "(AUTH_MFA_REQUIRED)."
        },
        403: {
            "description": "Wrong role (AUTH_INSUFFICIENT_ROLE), missing permission, "
            "or grade deadline passed (GRADE_DEADLINE_PASSED)."
        },
        404: {
            "description": "RESOURCE_NOT_FOUND when the section does not exist or is "
            "not assigned to the caller."
        },
    },
)


@router.get(
    "/{section_id}/grades",
    response_model=SectionGradesRead,
    operation_id="get_faculty_section_grades",
    dependencies=[Depends(require_permission(READ_COURSE_SECTION))],
)
async def get_section_grades(
    section_id: uuid.UUID = Path(
        ...,
        description="Course section UUID assigned to the faculty member.",
        openapi_examples={
            "default": {
                "summary": "Section UUID placeholder",
                "value": "00000000-0000-0000-0000-000000000000",
            }
        },
    ),
    user: CurrentUser = Depends(require_faculty_session),
    db: AsyncSession = Depends(get_db),
) -> SectionGradesRead:
    """Return enrolled students' marks, state, version, and submission deadline."""
    return await service.get_section_grades(
        db, faculty_id=user.id, section_id=section_id
    )


@router.get(
    "/{section_id}/grade-deadline",
    response_model=GradeDeadlineRead,
    operation_id="get_faculty_section_grade_deadline",
    dependencies=[Depends(require_permission(READ_COURSE_SECTION))],
)
async def get_section_grade_deadline(
    section_id: uuid.UUID = Path(
        ...,
        description="Course section UUID assigned to the faculty member.",
        openapi_examples={
            "default": {
                "summary": "Section UUID placeholder",
                "value": "00000000-0000-0000-0000-000000000000",
            }
        },
    ),
    user: CurrentUser = Depends(require_faculty_session),
    db: AsyncSession = Depends(get_db),
) -> GradeDeadlineRead:
    """Return the section's grade submission deadline and whether it has passed."""
    return await service.get_section_grade_deadline(
        db, faculty_id=user.id, section_id=section_id
    )


@router.put(
    "/{section_id}/grades/draft",
    response_model=SectionGradesRead,
    operation_id="save_faculty_section_grade_draft",
    dependencies=[Depends(require_permission(UPDATE_GRADE))],
    responses={
        409: {
            "description": "GRADES_LOCKED if already finalized, or VERSION_CONFLICT "
            "if the draft version is stale."
        },
        422: {
            "description": "VALIDATION_ERROR naming offending student row(s) when "
            "marks are outside 1.00 to 5.00 and not INC."
        },
    },
)
async def save_section_grade_draft(
    request: Request,
    section_id: uuid.UUID = Path(
        ...,
        description="Course section UUID assigned to the faculty member.",
        openapi_examples={
            "default": {
                "summary": "Section UUID placeholder",
                "value": "00000000-0000-0000-0000-000000000000",
            }
        },
    ),
    body: SaveGradeDraftRequest = Body(
        ...,
        openapi_examples={
            "default": {
                "summary": "Sample draft payload",
                "value": {
                    "version": 0,
                    "grades": [
                        {
                            "enrollmentId": "00000000-0000-0000-0000-000000000001",
                            "finalGrade": "1.25",
                            "midtermGrade": "1.50",
                            "remarks": "Draft mark",
                        }
                    ],
                },
            }
        },
    ),
    user: CurrentUser = Depends(require_faculty_session),
    db: AsyncSession = Depends(get_db),
) -> SectionGradesRead:
    """Save draft marks for a section assigned to the caller."""
    return await service.save_section_grade_draft(
        db,
        faculty_id=user.id,
        section_id=section_id,
        payload=body,
        client_ip=client_ip_or_none(request),
    )


@router.post(
    "/{section_id}/grades/finalize",
    response_model=FinalizeGradesRead,
    operation_id="finalize_faculty_section_grades",
    dependencies=[Depends(require_permission(SUBMIT_GRADE))],
    responses={
        403: {
            "description": "GRADE_DEADLINE_PASSED with deadline when the submission "
            "deadline has passed."
        },
        409: {
            "description": "GRADES_LOCKED if already finalized, or VERSION_CONFLICT "
            "if a supplied version is stale."
        },
        422: {
            "description": "VALIDATION_ERROR naming student row(s) with missing or "
            "invalid marks; the section remains in DRAFT."
        },
    },
)
async def finalize_section_grades(
    request: Request,
    section_id: uuid.UUID = Path(
        ...,
        description="Course section UUID assigned to the faculty member.",
        openapi_examples={
            "default": {
                "summary": "Section UUID placeholder",
                "value": "00000000-0000-0000-0000-000000000000",
            }
        },
    ),
    body: FinalizeGradesRequest | None = Body(
        default=None,
        openapi_examples={
            "default": {
                "summary": "Sample finalize payload",
                "value": {
                    "version": 0,
                },
            }
        },
    ),
    user: CurrentUser = Depends(require_faculty_session),
    db: AsyncSession = Depends(get_db),
) -> FinalizeGradesRead:
    """Finalize and lock section grades once, notifying each enrolled student."""
    return await service.finalize_section_grades(
        db,
        faculty_id=user.id,
        section_id=section_id,
        payload=body,
        client_ip=client_ip_or_none(request),
    )
