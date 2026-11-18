"""HTTP routes for enrollment reporting and document export."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.reports import service
from cbpupsis_shared.domains.reports.dependencies import (
    ReportScope,
    require_report_scope,
)
from cbpupsis_shared.domains.reports.schemas import EnrollmentReportRead

router = APIRouter()


@router.get(
    "/enrollment",
    response_model=EnrollmentReportRead,
    operation_id="get_admin_enrollment_report",
)
async def get_enrollment_report(
    term_id: str | None = Query(default=None, alias="termId"),
    program_code: str | None = Query(default=None, alias="programCode"),
    status: str | None = Query(default=None),
    group_by: str = Query(default="program", alias="groupBy"),
    scope: ReportScope = Depends(require_report_scope),
    db: AsyncSession = Depends(get_db),
) -> EnrollmentReportRead:
    """Return enrollment counts grouped by field, filtered to caller's role scope."""
    return await service.generate_enrollment_report(
        db,
        term_id=term_id,
        group_by=group_by,
        program_code=program_code,
        status=status,
        scope=scope,
    )


@router.get(
    "/enrollment/export",
    operation_id="export_admin_enrollment_report",
)
async def export_enrollment_report(
    term_id: str | None = Query(default=None, alias="termId"),
    program_code: str | None = Query(default=None, alias="programCode"),
    status: str | None = Query(default=None),
    group_by: str = Query(default="program", alias="groupBy"),
    export_format: str = Query(default="csv", alias="format"),
    scope: ReportScope = Depends(require_report_scope),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Export enrollment report numbers as a downloadable CSV or PDF file."""
    content, media_type, filename = await service.export_enrollment_report(
        db,
        term_id=term_id,
        group_by=group_by,
        program_code=program_code,
        status=status,
        export_format=export_format,
        scope=scope,
    )
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
