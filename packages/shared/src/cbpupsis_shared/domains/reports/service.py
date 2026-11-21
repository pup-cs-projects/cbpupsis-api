"""Service layer for enrollment report aggregation and document export."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.academics import client as academics_client
from cbpupsis_shared.domains.enrollment import client as enrollment_client
from cbpupsis_shared.domains.reports.constants import (
    VALID_EXPORT_FORMATS,
    VALID_GROUP_BY,
)
from cbpupsis_shared.domains.reports.dependencies import ReportScope
from cbpupsis_shared.domains.reports.exceptions import (
    InvalidInputError,
    ResourceNotFoundError,
)
from cbpupsis_shared.domains.reports.exports import render_csv, render_pdf
from cbpupsis_shared.domains.reports.schemas import (
    EnrollmentReportRead,
    EnrollmentReportRow,
)


def _parse_term_id(term_id: str | uuid.UUID | None) -> uuid.UUID:
    if term_id is None or (isinstance(term_id, str) and not term_id.strip()):
        raise InvalidInputError("termId is required")
    if isinstance(term_id, uuid.UUID):
        return term_id
    try:
        return uuid.UUID(str(term_id).strip())
    except (ValueError, TypeError, AttributeError):
        raise InvalidInputError(f"Invalid termId: {term_id}") from None


async def generate_enrollment_report(
    db: AsyncSession,
    *,
    term_id: str | uuid.UUID | None,
    group_by: str = "program",
    program_code: str | None = None,
    status: str | None = None,
    scope: ReportScope,
) -> EnrollmentReportRead:
    """Generate enrollment statistics rows and total filtered by caller scope."""
    term_uuid = _parse_term_id(term_id)
    term = await academics_client.get_term_by_id(db, term_uuid)
    if term is None:
        raise ResourceNotFoundError(f"Unknown termId: {term_uuid}")

    if group_by not in VALID_GROUP_BY:
        raise InvalidInputError(f"Unknown groupBy: {group_by}")

    department = None if scope.is_all else scope.department_id
    college = None if scope.is_all else scope.college_id

    raw_counts = await enrollment_client.get_enrollment_report_counts(
        db,
        term_id=term_uuid,
        group_by=group_by,
        program_code=program_code,
        status=status,
        department=department,
        college=college,
    )

    rows = [EnrollmentReportRow(group=g, count=c) for g, c in raw_counts]
    total = sum(r.count for r in rows)
    return EnrollmentReportRead(rows=rows, total=total)


async def export_enrollment_report(
    db: AsyncSession,
    *,
    term_id: str | uuid.UUID | None,
    group_by: str = "program",
    program_code: str | None = None,
    status: str | None = None,
    export_format: str = "csv",
    scope: ReportScope,
) -> tuple[bytes, str, str]:
    """Render enrollment report numbers to CSV or PDF file."""
    format_lower = (export_format or "csv").lower()
    if format_lower not in VALID_EXPORT_FORMATS:
        raise InvalidInputError(f"Unknown export format: {export_format}")

    term_uuid = _parse_term_id(term_id)
    term = await academics_client.get_term_by_id(db, term_uuid)
    if term is None:
        raise ResourceNotFoundError(f"Unknown termId: {term_uuid}")

    report = await generate_enrollment_report(
        db,
        term_id=term_uuid,
        group_by=group_by,
        program_code=program_code,
        status=status,
        scope=scope,
    )

    if format_lower == "csv":
        content = render_csv(report)
        return content, "text/csv; charset=utf-8", "enrollment-report.csv"

    term_label = f"{term.academic_year} {term.semester}"
    content = render_pdf(report, term_label=term_label)
    return content, "application/pdf", "enrollment-report.pdf"
