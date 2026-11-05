"""Owner-scoped enrollment queries; no enrollment mutations.

Only this module builds SQL or calls into the session for term enrollments
and course registrations.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import Uuid, bindparam, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.enrollment import (
    OverloadRequest,
    StudentCourseEnrollment,
    TermEnrollment,
)


async def get_term_enrollment(
    db: AsyncSession, student_id: uuid.UUID, academic_term_id: uuid.UUID
) -> TermEnrollment | None:
    """Return student term enrollment record.

    SQL::

        SELECT term_enrollments.id, term_enrollments.student_id,
               term_enrollments.academic_term_id, term_enrollments.enrollment_status,
               term_enrollments.cor_downloaded, term_enrollments.enrolled_at
        FROM term_enrollments
        WHERE term_enrollments.student_id = :student_id
          AND term_enrollments.academic_term_id = :academic_term_id
        LIMIT 1
    """
    stmt = (
        select(TermEnrollment)
        .where(
            TermEnrollment.student_id == student_id,
            TermEnrollment.academic_term_id == academic_term_id,
        )
        .limit(1)
    )
    return await db.scalar(stmt)


async def get_enrolled_units_and_course_count(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    academic_term_id: uuid.UUID,
) -> tuple[Decimal, int]:
    """Compute enrolled unit count and course count in one bounded query.

    SQL::

        SELECT COALESCE(SUM(c.lecture_units + c.lab_units), 0) AS total_units,
               COUNT(sce.id) AS course_count
        FROM student_course_enrollments sce
        JOIN term_enrollments te ON te.id = sce.term_enrollment_id
        JOIN course_sections cs ON cs.id = sce.section_id
        JOIN courses c ON c.id = cs.course_id
        WHERE te.student_id = :student_id
          AND sce.academic_term_id = :academic_term_id
          AND sce.status = 'enrolled'
    """
    stmt = text(
        """
            SELECT COALESCE(SUM(c.lecture_units + c.lab_units), 0) AS total_units,
                   COUNT(sce.id) AS course_count
            FROM student_course_enrollments sce
            JOIN term_enrollments te ON te.id = sce.term_enrollment_id
            JOIN course_sections cs ON cs.id = sce.section_id
            JOIN courses c ON c.id = cs.course_id
            WHERE te.student_id = :student_id
              AND sce.academic_term_id = :academic_term_id
              AND sce.status = 'enrolled'
            """
    ).bindparams(
        bindparam("student_id", type_=Uuid),
        bindparam("academic_term_id", type_=Uuid),
    )
    result = await db.execute(
        stmt,
        {"student_id": student_id, "academic_term_id": academic_term_id},
    )
    row = result.first()
    if not row:
        return (Decimal("0"), 0)
    return (Decimal(str(row[0])), int(row[1]))


async def get_approved_overload_units(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    academic_term_id: uuid.UUID,
) -> Decimal:
    """Return sum of approved overload units for student and term.

    SQL::

        SELECT COALESCE(SUM(overload_requests.requested_units), 0)
        FROM overload_requests
        WHERE overload_requests.student_id = :student_id
          AND overload_requests.academic_term_id = :academic_term_id
          AND overload_requests.state IN ('APPROVED', 'APPROVED_DEAN')
    """
    stmt = select(func.coalesce(func.sum(OverloadRequest.requested_units), 0)).where(
        OverloadRequest.student_id == student_id,
        OverloadRequest.academic_term_id == academic_term_id,
        OverloadRequest.state.in_(["APPROVED", "APPROVED_DEAN", "approved"]),
    )
    val = await db.scalar(stmt)
    return Decimal(str(val)) if val is not None else Decimal("0")


async def list_record_enrollments(db: AsyncSession, student_id: uuid.UUID):
    """Retain historical attempts, including withdrawn courses with posted marks.

    SQL::

        SELECT e.id, e.academic_term_id, e.section_id
        FROM student_course_enrollments e
        JOIN term_enrollments t ON e.term_enrollment_id = t.id
          AND e.academic_term_id = t.academic_term_id
        WHERE t.student_id = :student_id;
    """
    result = await db.execute(
        select(
            StudentCourseEnrollment.id,
            StudentCourseEnrollment.academic_term_id,
            StudentCourseEnrollment.section_id,
        )
        .join(
            TermEnrollment,
            (StudentCourseEnrollment.term_enrollment_id == TermEnrollment.id)
            & (
                StudentCourseEnrollment.academic_term_id
                == TermEnrollment.academic_term_id
            ),
        )
        .where(TermEnrollment.student_id == student_id)
    )
    return result.all()


async def enrolled_sections(
    db: AsyncSession, student_id: uuid.UUID, term_id: uuid.UUID
):
    """Read officially enrolled courses; pending/dropped records are not classes.

    SQL::

        SELECT sce.* FROM student_course_enrollments AS sce
        JOIN term_enrollments AS te ON te.id = sce.term_enrollment_id
        WHERE te.student_id = :student_id AND te.academic_term_id = :term_id
          AND sce.academic_term_id = :term_id
          AND te.enrollment_status = 'enrolled' AND sce.status = 'enrolled'
        ORDER BY sce.section_id LIMIT 101;
    """
    return list(
        await db.scalars(
            select(StudentCourseEnrollment)
            .join(
                TermEnrollment,
                TermEnrollment.id == StudentCourseEnrollment.term_enrollment_id,
            )
            .where(
                TermEnrollment.student_id == student_id,
                TermEnrollment.academic_term_id == term_id,
                StudentCourseEnrollment.academic_term_id == term_id,
                TermEnrollment.enrollment_status == "enrolled",
                StudentCourseEnrollment.status == "enrolled",
            )
            .order_by(StudentCourseEnrollment.section_id)
            .limit(101)
        )
    )


async def get_enrollment_report_counts(
    db: AsyncSession,
    *,
    term_id: uuid.UUID,
    group_by: str,
    program_code: str | None = None,
    status: str | None = None,
    department: str | None = None,
    college: str | None = None,
) -> list[tuple[str, int]]:
    """Return counts of enrolled students for a term, grouped by the selected field.

    SQL::

        SELECT COALESCE(p.code, 'Unknown') AS group_name, COUNT(*) AS count
        FROM term_enrollments te
        JOIN student_profiles sp ON sp.user_id = te.student_id
        JOIN programs p ON p.id = sp.program_id
        LEFT JOIN user_profiles up ON up.user_id = te.student_id
        WHERE te.academic_term_id = :term_id
          AND (:program_code IS NULL OR lower(p.code) = lower(:program_code))
          AND (:status IS NULL OR lower(te.enrollment_status) = lower(:status))
          AND (:department IS NULL OR p.department = :department)
          AND (:college IS NULL OR p.college = :college)
        GROUP BY group_name
        ORDER BY group_name ASC;
    """
    if group_by == "program":
        group_expr = "COALESCE(p.code, 'Unknown')"
    elif group_by == "status":
        group_expr = "COALESCE(te.enrollment_status, 'unknown')"
    elif group_by in ("yearLevel", "year_level"):
        group_expr = "COALESCE(CAST(sp.year_level AS TEXT), 'Unassigned')"
    elif group_by == "sex":
        group_expr = "COALESCE(up.gender, 'Unspecified')"
    elif group_by == "section":
        group_expr = "COALESCE(sp.section, 'Unassigned')"
    else:
        raise ValueError(f"Unsupported group_by: {group_by}")

    query_parts = [
        f"SELECT {group_expr} AS group_name, COUNT(*) AS count",
        "FROM term_enrollments te",
        "JOIN student_profiles sp ON sp.user_id = te.student_id",
        "JOIN programs p ON p.id = sp.program_id",
        "LEFT JOIN user_profiles up ON up.user_id = te.student_id",
        "WHERE te.academic_term_id = :term_id",
    ]
    params: dict[str, object] = {"term_id": term_id}

    if program_code:
        query_parts.append("AND lower(p.code) = lower(:program_code)")
        params["program_code"] = program_code

    if status:
        query_parts.append("AND lower(te.enrollment_status) = lower(:status)")
        params["status"] = status

    if department:
        query_parts.append("AND p.department = :department")
        params["department"] = department

    if college:
        query_parts.append("AND p.college = :college")
        params["college"] = college

    query_parts.append(f"GROUP BY {group_expr}")
    query_parts.append(f"ORDER BY {group_expr} ASC")

    stmt = text("\n".join(query_parts)).bindparams(
        bindparam("term_id", type_=Uuid),
    )
    result = await db.execute(stmt, params)
    return [(str(row[0]), int(row[1])) for row in result.all()]
