"""Owner-scoped enrollment reads and transaction-compatible COR persistence.

Only this module builds SQL or calls into the session for term enrollments
and course registrations.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import Uuid, bindparam, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.enrollment import (
    CorIssuance,
    OverloadRequest,
    StudentCourseEnrollment,
    TermEnrollment,
)


async def begin_certificate_snapshot(db):
    """First statement of the owner's transaction for coherent domain reads.

    SQL::

        SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;
    """
    if db.bind.dialect.name == "postgresql":
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))


async def certificate_snapshot_is_consistent(db):
    """Cannot upgrade an active snapshot.

    SQL::

        SHOW transaction_isolation;
    """
    if db.bind.dialect.name != "postgresql":
        return True
    return await db.scalar(text("SHOW transaction_isolation")) in {
        "repeatable read",
        "serializable",
    }


async def enrollment(db, student_id, term_id, *, lock=False):
    """Lock or read a session-owned term enrollment.

    SQL::

        SELECT * FROM term_enrollments
        WHERE student_id=:s AND academic_term_id=:t [FOR UPDATE];
    """
    stmt = select(TermEnrollment).where(
        TermEnrollment.student_id == student_id,
        TermEnrollment.academic_term_id == term_id,
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return await db.scalar(stmt)


async def current(db, enrollment_id, *, lock=False):
    """Read the single current issuance, optionally locking it.

    SQL::

        SELECT * FROM cor_issuances
        WHERE term_enrollment_id=:e AND status='CURRENT' [FOR UPDATE];
    """
    stmt = select(CorIssuance).where(
        CorIssuance.term_enrollment_id == enrollment_id, CorIssuance.status == "CURRENT"
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return await db.scalar(stmt)


async def registrations(db, enrollment_id, term_id):
    """Read registrations with an overflow sentinel.

    SQL::

        SELECT * FROM student_course_enrollments
        WHERE term_enrollment_id=:e AND academic_term_id=:t AND status='enrolled'
        ORDER BY section_id LIMIT 101;
    """
    return list(
        await db.scalars(
            select(StudentCourseEnrollment)
            .where(
                StudentCourseEnrollment.term_enrollment_id == enrollment_id,
                StudentCourseEnrollment.academic_term_id == term_id,
                StudentCourseEnrollment.status == "enrolled",
            )
            .order_by(StudentCourseEnrollment.section_id)
            .limit(101)
        )
    )


async def by_hash(db, digest):
    """Unique indexed token lookup.

    SQL::

        SELECT * FROM cor_issuances WHERE verification_token_hash=:h;
    """
    return await db.scalar(
        select(CorIssuance).where(CorIssuance.verification_token_hash == digest)
    )


def add(db, **fields):
    """Stage encrypted content and hash without committing.

    SQL::

        INSERT INTO cor_issuances (id, student_id, term_enrollment_id,
            verification_token_hash, token_envelope, snapshot_envelope,
            status, issued_at) VALUES (:id, :student, :enrollment,
            :hash, :token_envelope, :snapshot_envelope, :status, :issued_at);
    """
    row = CorIssuance(**fields)
    db.add(row)
    return row


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
          AND te.enrollment_status IN ('enrolled', 'OFFICIALLY_ENROLLED')
          AND sce.status = 'enrolled'
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
                TermEnrollment.enrollment_status.in_(
                    ["enrolled", "OFFICIALLY_ENROLLED"]
                ),
                StudentCourseEnrollment.status == "enrolled",
            )
            .order_by(StudentCourseEnrollment.section_id)
            .limit(101)
        )
    )
