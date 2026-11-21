"""HTTP routing for faculty sections, mounted at /api/v1/faculty/sections.

Both checks on every endpoint: the coarse ``ReadCourseSection`` permission plus
an MFA-completed faculty session here, and the ownership check in the service.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_faculty.domains.faculty_auth.dependencies import (
    require_faculty_session,
)
from cbpupsis_api_faculty.domains.faculty_sections import service
from cbpupsis_api_faculty.domains.faculty_sections.schemas import (
    CourseSectionRead,
    CourseSectionRosterRead,
    EnrollmentStatus,
    FacultyStudentRead,
    FacultyStudentRecordRead,
    GradeStatus,
)
from cbpupsis_core.pagination import Page
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.iam.dependencies import require_permission
from cbpupsis_shared.domains.sections.constants import READ_COURSE_SECTION

router = APIRouter(
    dependencies=[
        Depends(require_faculty_session),
        Depends(require_permission(READ_COURSE_SECTION)),
    ]
)


@router.get("", response_model=Page[CourseSectionRead])
async def list_sections(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(require_faculty_session),
    db: AsyncSession = Depends(get_db),
) -> Page[CourseSectionRead]:
    """List the sections assigned to you."""
    return await service.list_own_sections(
        db, faculty_id=user.id, limit=limit, offset=offset
    )


@router.get("/{section_id}/roster", response_model=CourseSectionRosterRead)
async def section_roster(
    section_id: uuid.UUID,
    enrollment_status: EnrollmentStatus | None = Query(
        default=None, alias="enrollmentStatus"
    ),
    grade_status: GradeStatus | None = Query(default=None, alias="gradeStatus"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(require_faculty_session),
    db: AsyncSession = Depends(get_db),
) -> CourseSectionRosterRead:
    """Return one of your sections with its roster; 404 for anyone else's."""
    return await service.get_section_roster(
        db,
        faculty_id=user.id,
        section_id=section_id,
        enrollment_status=enrollment_status,
        grade_status=grade_status,
        limit=limit,
        offset=offset,
    )


@router.get("/{section_id}/roster/export", response_class=Response)
async def export_section_roster(
    section_id: uuid.UUID,
    format: Literal["csv"] = Query(default="csv"),
    enrollment_status: EnrollmentStatus | None = Query(
        default=None, alias="enrollmentStatus"
    ),
    grade_status: GradeStatus | None = Query(default=None, alias="gradeStatus"),
    user: CurrentUser = Depends(require_faculty_session),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Export every student matching the same roster filters."""
    content = await service.export_section_roster_csv(
        db,
        faculty_id=user.id,
        section_id=section_id,
        enrollment_status=enrollment_status,
        grade_status=grade_status,
    )
    return Response(
        content=content,
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="section-{section_id}-roster.csv"'
            ),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{section_id}/roster/{student_id}", response_model=FacultyStudentRead)
async def section_student(
    section_id: uuid.UUID,
    student_id: uuid.UUID,
    user: CurrentUser = Depends(require_faculty_session),
    db: AsyncSession = Depends(get_db),
) -> FacultyStudentRead:
    """Return the teaching-safe details of one student in the section."""
    return await service.get_section_student(
        db,
        faculty_id=user.id,
        section_id=section_id,
        student_id=student_id,
    )


@router.get(
    "/{section_id}/roster/{student_id}/record",
    response_model=FacultyStudentRecordRead,
)
async def section_student_record(
    section_id: uuid.UUID,
    student_id: uuid.UUID,
    user: CurrentUser = Depends(require_faculty_session),
    db: AsyncSession = Depends(get_db),
) -> FacultyStudentRecordRead:
    """Disclose course-scoped results and write the required audit row."""
    return await service.get_section_student_record(
        db,
        faculty_id=user.id,
        section_id=section_id,
        student_id=student_id,
    )
