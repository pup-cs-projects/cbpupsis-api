"""Owner-scoped enrollment queries; no enrollment mutations.

Only this module builds SQL or calls into the session for term enrollments
and course registrations.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from decimal import Decimal

from sqlalchemy import Uuid, bindparam, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.enrollment import (
    OverloadRequest,
    StudentCourseEnrollment,
    TermEnrollment,
)
from cbpupsis_shared.domains.enrollment.constants import (
    ACTIVE_COURSE_ENROLLMENT_STATUSES,
    ACTIVE_TERM_ENROLLMENT_STATUSES,
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


def add_overload_request(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    academic_term_id: uuid.UUID,
    requested_units: int,
    justification: str,
    state: str,
) -> OverloadRequest:
    """Stage one student overload request in the caller's transaction.

    SQL::

        INSERT INTO overload_requests
            (student_id, academic_term_id, requested_units, justification,
             state, id)
        VALUES (:student_id, :academic_term_id, :requested_units,
                :justification, :state, :id);
    """
    row = OverloadRequest(
        student_id=student_id,
        academic_term_id=academic_term_id,
        requested_units=requested_units,
        justification=justification,
        state=state,
    )
    db.add(row)
    return row


async def list_overload_requests(db: AsyncSession) -> list[OverloadRequest]:
    """List overload requests for service-side identity and scope composition.

    SQL::

        SELECT * FROM overload_requests
        ORDER BY created_at DESC, id DESC LIMIT 1001;
    """
    return list(
        await db.scalars(
            select(OverloadRequest)
            .order_by(OverloadRequest.created_at.desc(), OverloadRequest.id.desc())
            .limit(1001)
            .execution_options(populate_existing=True)
        )
    )


async def get_overload_request_for_update(
    db: AsyncSession,
    *,
    overload_request_id: uuid.UUID,
) -> OverloadRequest | None:
    """Lock one overload request through the caller's transaction.

    SQL::

        SELECT * FROM overload_requests
        WHERE id = :overload_request_id FOR UPDATE;
    """
    return await db.scalar(
        select(OverloadRequest)
        .where(OverloadRequest.id == overload_request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def get_overload_request(
    db: AsyncSession, overload_request_id: uuid.UUID
) -> OverloadRequest | None:
    """Read one overload request by id.

    SQL::

        SELECT * FROM overload_requests WHERE id = :overload_request_id;
    """
    return await db.scalar(
        select(OverloadRequest).where(OverloadRequest.id == overload_request_id)
    )


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
          AND te.enrollment_status IN ('enrolled', 'ENROLLED_PENDING')
          AND sce.status IN ('enrolled', 'ENROLLED_PENDING')
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
                TermEnrollment.enrollment_status.in_(ACTIVE_TERM_ENROLLMENT_STATUSES),
                StudentCourseEnrollment.status.in_(ACTIVE_COURSE_ENROLLMENT_STATUSES),
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


async def count_active_section_enrollments(
    db: AsyncSession, section_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Count occupied seats without exposing any student's selection.

    SQL::

        SELECT student_course_enrollments.section_id,
               count(student_course_enrollments.id)
        FROM student_course_enrollments
        JOIN term_enrollments
          ON term_enrollments.id = student_course_enrollments.term_enrollment_id
        WHERE student_course_enrollments.section_id IN (:section_ids)
          AND student_course_enrollments.status
              IN ('enrolled', 'ENROLLED_PENDING')
          AND term_enrollments.enrollment_status
              IN ('enrolled', 'ENROLLED_PENDING')
        GROUP BY student_course_enrollments.section_id;
    """
    if not section_ids:
        return {}
    result = await db.execute(
        select(
            StudentCourseEnrollment.section_id,
            func.count(StudentCourseEnrollment.id),
        )
        .join(
            TermEnrollment,
            TermEnrollment.id == StudentCourseEnrollment.term_enrollment_id,
        )
        .where(
            StudentCourseEnrollment.section_id.in_(section_ids),
            StudentCourseEnrollment.status.in_(ACTIVE_COURSE_ENROLLMENT_STATUSES),
            TermEnrollment.enrollment_status.in_(ACTIVE_TERM_ENROLLMENT_STATUSES),
        )
        .group_by(StudentCourseEnrollment.section_id)
    )
    return {section_id: int(count) for section_id, count in result.all()}


async def list_term_enrollment_sections(
    db: AsyncSession, term_enrollment_id: uuid.UUID
) -> list[StudentCourseEnrollment]:
    """Return the seat rows belonging to one idempotent submission.

    SQL::

        SELECT * FROM student_course_enrollments
        WHERE term_enrollment_id = :term_enrollment_id
        ORDER BY section_id;
    """
    rows = await db.scalars(
        select(StudentCourseEnrollment)
        .where(StudentCourseEnrollment.term_enrollment_id == term_enrollment_id)
        .order_by(StudentCourseEnrollment.section_id)
    )
    return list(rows)


def add_term_enrollment(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    academic_term_id: uuid.UUID,
    status: str,
) -> TermEnrollment:
    """Stage the unique student-term enrollment.

    On the service's commit::

        INSERT INTO term_enrollments
            (student_id, academic_term_id, enrollment_status, id)
        VALUES (:student_id, :academic_term_id, :status, :id);
    """
    row = TermEnrollment(
        student_id=student_id,
        academic_term_id=academic_term_id,
        enrollment_status=status,
    )
    db.add(row)
    return row


def add_course_enrollments(
    db: AsyncSession,
    *,
    term_enrollment_id: uuid.UUID,
    academic_term_id: uuid.UUID,
    section_ids: Collection[uuid.UUID],
) -> list[StudentCourseEnrollment]:
    """Stage the selected seat rows in the caller's transaction.

    On the service's commit::

        INSERT INTO student_course_enrollments
            (term_enrollment_id, academic_term_id, section_id, status, id)
        VALUES (:term_enrollment_id, :academic_term_id, :section_id,
                'enrolled', :id);
    """
    rows = [
        StudentCourseEnrollment(
            term_enrollment_id=term_enrollment_id,
            academic_term_id=academic_term_id,
            section_id=section_id,
            status="enrolled",
        )
        for section_id in section_ids
    ]
    db.add_all(rows)
    return rows
