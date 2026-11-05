"""Owner-scoped enrollment queries; no enrollment mutations.

Only this module builds SQL or calls into the session for term enrollments
and course registrations.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Collection
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Uuid, bindparam, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cbpupsis_database.models.enrollment import (
    AddDropRequest,
    AddDropRequestItem,
    CorIssuance,
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


async def get_latest_term_enrollment(
    db: AsyncSession, student_id: uuid.UUID
) -> TermEnrollment | None:
    """Return the most recent active term enrollment for a student.

    SQL::

        SELECT * FROM term_enrollments
        WHERE student_id = :student_id
          AND enrollment_status IN ('enrolled', 'ENROLLED_PENDING')
        ORDER BY enrolled_at DESC, id DESC
        LIMIT 1;
    """
    stmt = (
        select(TermEnrollment)
        .where(
            TermEnrollment.student_id == student_id,
            TermEnrollment.enrollment_status.in_(ACTIVE_TERM_ENROLLMENT_STATUSES),
        )
        .order_by(TermEnrollment.enrolled_at.desc(), TermEnrollment.id.desc())
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


def add_add_drop_request(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    academic_term_id: uuid.UUID,
    comments: str | None,
    units_before: Decimal,
    units_after: Decimal,
    add_section_ids: list[uuid.UUID],
    drop_sections: list[tuple[uuid.UUID, str]],
) -> AddDropRequest:
    """Stage one composite add and drop request with individual item rows.

    On the service's commit::

        INSERT INTO add_drop_requests
            (id, student_id, academic_term_id, comments,
             units_before, units_after, state, version)
        VALUES
            (:id, :student_id, :academic_term_id, :comments,
             :units_before, :units_after, 'PENDING', 1);

        INSERT INTO add_drop_request_items
            (id, request_id, section_id, action, reason)
        VALUES
            (:id, :request_id, :section_id, :action, :reason);
    """
    request = AddDropRequest(
        student_id=student_id,
        academic_term_id=academic_term_id,
        comments=comments,
        units_before=units_before,
        units_after=units_after,
        state="PENDING",
        version=1,
    )
    db.add(request)
    for section_id in add_section_ids:
        item = AddDropRequestItem(
            request=request,
            section_id=section_id,
            action="ADD",
            reason=None,
        )
        db.add(item)
    for section_id, reason in drop_sections:
        item = AddDropRequestItem(
            request=request,
            section_id=section_id,
            action="DROP",
            reason=reason,
        )
        db.add(item)
    return request


async def get_add_drop_request(
    db: AsyncSession, request_id: uuid.UUID
) -> AddDropRequest | None:
    """Read one add/drop request with items eagerly loaded.

    SQL::

        SELECT * FROM add_drop_requests
        WHERE id = :request_id;
    """
    stmt = (
        select(AddDropRequest)
        .options(selectinload(AddDropRequest.items))
        .where(AddDropRequest.id == request_id)
    )
    return await db.scalar(stmt)


async def get_add_drop_request_for_update(
    db: AsyncSession, request_id: uuid.UUID
) -> AddDropRequest | None:
    """Lock one add/drop request for decision review.

    SQL::

        SELECT * FROM add_drop_requests
        WHERE id = :request_id
        FOR UPDATE;
    """
    stmt = (
        select(AddDropRequest)
        .options(selectinload(AddDropRequest.items))
        .where(AddDropRequest.id == request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return await db.scalar(stmt)


async def list_student_add_drop_requests(
    db: AsyncSession, student_id: uuid.UUID
) -> list[AddDropRequest]:
    """List caller's own add/drop requests, newest first.

    SQL::

        SELECT * FROM add_drop_requests
        WHERE student_id = :student_id
        ORDER BY created_at DESC, id DESC;
    """
    stmt = (
        select(AddDropRequest)
        .options(selectinload(AddDropRequest.items))
        .where(AddDropRequest.student_id == student_id)
        .order_by(AddDropRequest.created_at.desc(), AddDropRequest.id.desc())
    )
    return list(await db.scalars(stmt))


async def list_all_add_drop_requests(db: AsyncSession) -> list[AddDropRequest]:
    """List all add/drop requests for admin scope filtering.

    SQL::

        SELECT * FROM add_drop_requests
        ORDER BY created_at DESC, id DESC
        LIMIT 1001;
    """
    stmt = (
        select(AddDropRequest)
        .options(selectinload(AddDropRequest.items))
        .order_by(AddDropRequest.created_at.desc(), AddDropRequest.id.desc())
        .limit(1001)
    )
    return list(await db.scalars(stmt))


async def get_student_course_enrollment(
    db: AsyncSession, term_enrollment_id: uuid.UUID, section_id: uuid.UUID
) -> StudentCourseEnrollment | None:
    """Fetch one section enrollment record for a student term.

    SQL::

        SELECT * FROM student_course_enrollments
        WHERE term_enrollment_id = :term_enrollment_id
          AND section_id = :section_id;
    """
    stmt = select(StudentCourseEnrollment).where(
        StudentCourseEnrollment.term_enrollment_id == term_enrollment_id,
        StudentCourseEnrollment.section_id == section_id,
    )
    return await db.scalar(stmt)


async def supersede_and_issue_cor(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    term_enrollment_id: uuid.UUID,
    now: datetime,
) -> CorIssuance:
    """Supersede existing CURRENT certificate and issue a fresh one.

    SQL::

        SELECT * FROM cor_issuances
        WHERE term_enrollment_id = :term_enrollment_id
          AND status = 'CURRENT';

    On the service's commit::

        UPDATE cor_issuances
        SET status = 'SUPERSEDED', superseded_at = :now
        WHERE id = :id;

        INSERT INTO cor_issuances
            (id, student_id, term_enrollment_id,
             verification_token_hash, status, issued_at)
        VALUES
            (:id, :student_id, :term_enrollment_id,
             :hash, 'CURRENT', :now);
    """
    existing = await db.scalars(
        select(CorIssuance).where(
            CorIssuance.term_enrollment_id == term_enrollment_id,
            CorIssuance.status == "CURRENT",
        )
    )
    for cor in existing:
        cor.status = "SUPERSEDED"
        cor.superseded_at = now

    token_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    new_cor = CorIssuance(
        student_id=student_id,
        term_enrollment_id=term_enrollment_id,
        verification_token_hash=token_hash,
        status="CURRENT",
        issued_at=now,
    )
    db.add(new_cor)
    return new_cor
