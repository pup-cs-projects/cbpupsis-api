"""Owner-scoped enrollment reads and transaction-compatible COR persistence.

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
    CorIssuance,
    CourseWaiver,
    OverloadRequest,
    StudentCourseEnrollment,
    TermEnrollment,
)
from cbpupsis_shared.domains.enrollment.constants import (
    ACTIVE_COURSE_ENROLLMENT_STATUSES,
    ACTIVE_TERM_ENROLLMENT_STATUSES,
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
          AND te.enrollment_status IN ('enrolled', 'ENROLLED_PENDING',
              'OFFICIALLY_ENROLLED')
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


async def active_registrations(db: AsyncSession, student_id, term_id):
    """Read active selected/official registrations, not dropped attempts.

    SQL::

        SELECT sce.* FROM student_course_enrollments sce
        JOIN term_enrollments te ON te.id = sce.term_enrollment_id
        WHERE te.student_id = :student AND te.academic_term_id = :term
          AND sce.academic_term_id = :term
          AND te.enrollment_status IN ('enrolled','ENROLLED_PENDING',
                                      'OFFICIALLY_ENROLLED')
          AND sce.status IN ('enrolled','ENROLLED_PENDING','OFFICIALLY_ENROLLED');
    """
    active = ("enrolled", "ENROLLED_PENDING", "OFFICIALLY_ENROLLED")
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
                TermEnrollment.enrollment_status.in_(active),
                StudentCourseEnrollment.status.in_(active),
            )
        )
    )


async def academic_waivers(db: AsyncSession, student_id, section_ids):
    """Read only the authenticated student's applicable edges.

    SQL::

        SELECT * FROM course_waivers WHERE student_id = :student
          AND section_id IN (:sections) ORDER BY created_at, id;
    """
    return list(
        await db.scalars(
            select(CourseWaiver)
            .where(
                CourseWaiver.student_id == student_id,
                CourseWaiver.section_id.in_(section_ids),
            )
            .order_by(CourseWaiver.created_at, CourseWaiver.id)
            .execution_options(populate_existing=True)
        )
    )


def stage_waiver(db: AsyncSession, **fields):
    """Stage filing; the caller commits audit and filing together.

    SQL::

        INSERT INTO course_waivers (id, student_id, section_id,
          prerequisite_course_code, condition, justification, state)
        VALUES (:id, :student, :section, :course, :condition, :justification,
                'PENDING_CHAIR');
    """
    row = CourseWaiver(id=uuid.uuid4(), state="PENDING_CHAIR", **fields)
    db.add(row)
    return row


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


async def scoped_waivers(db: AsyncSession, student_ids, *, limit, offset):
    """Page through authorized students only; None is Registrar's broad scope.

    SQL::

        SELECT count(*) FROM course_waivers WHERE student_id IN (:scope);
        SELECT * FROM course_waivers WHERE student_id IN (:scope)
        ORDER BY created_at DESC, id LIMIT :limit OFFSET :offset;
    """
    filters = [] if student_ids is None else [CourseWaiver.student_id.in_(student_ids)]
    total = await db.scalar(
        select(func.count()).select_from(CourseWaiver).where(*filters)
    )
    rows = list(
        await db.scalars(
            select(CourseWaiver)
            .where(*filters)
            .order_by(CourseWaiver.created_at.desc(), CourseWaiver.id)
            .limit(limit)
            .offset(offset)
        )
    )
    return rows, total or 0


async def lock_scoped_waiver(db: AsyncSession, waiver_id, student_ids):
    """Scope-filter before locking, and refresh identity-map state after waiting.

    SQL::

        SELECT * FROM course_waivers WHERE id = :id AND student_id IN (:scope)
        FOR UPDATE;
    """
    filters = [] if student_ids is None else [CourseWaiver.student_id.in_(student_ids)]
    return await db.scalar(
        select(CourseWaiver)
        .where(CourseWaiver.id == waiver_id, *filters)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def stage_waiver_decision(row, **fields):
    """Change only the locked row in the caller's transaction.

    SQL::

        UPDATE course_waivers SET state=:state, review_remarks=:remarks,
          chair_approved_by=:chair, chair_approved_at=:chair_at,
          dean_approved_by=:dean, dean_approved_at=:dean_at,
          registrar_approved_by=:registrar, vpaa_approved_at=:vpaa_at,
          vpaa_token=:reference WHERE id=:id;
    """
    for key, value in fields.items():
        setattr(row, key, value)


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
