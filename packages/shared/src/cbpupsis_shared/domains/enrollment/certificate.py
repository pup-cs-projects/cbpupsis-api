"""Atomic COR operations and immutable reads, independent of submit/add-drop."""

import re
import secrets
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError

from cbpupsis_core import logged
from cbpupsis_core.config import settings
from cbpupsis_shared.domains.academics import client as academics
from cbpupsis_shared.domains.audit import client as audit
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.billing import client as billing
from cbpupsis_shared.domains.billing.schemas import StoredAssessment
from cbpupsis_shared.domains.curriculum import client as curriculum
from cbpupsis_shared.domains.enrollment import academic_verdict, repository
from cbpupsis_shared.domains.enrollment.certificate_schemas import (
    CertificateCourse,
    CertificateMeeting,
    CertificateMetadata,
    CertificateRead,
    CertificateTerm,
    VerificationRead,
    VerificationTerm,
)
from cbpupsis_shared.domains.enrollment.certificate_security import crypt, token_hash
from cbpupsis_shared.domains.enrollment.exceptions import (
    CertificateUnavailable,
    CorNotEnrolled,
    CorNotFinal,
)
from cbpupsis_shared.domains.sections import client as sections
from cbpupsis_shared.domains.users import client as users

OFFICIAL = {"enrolled", "OFFICIALLY_ENROLLED"}
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def _metadata(row):
    issued_at = row.issued_at
    if issued_at.tzinfo is None:  # SQLite does not retain timezone information.
        issued_at = issued_at.replace(tzinfo=UTC)
    return CertificateMetadata(
        certificateId=row.id, issuedAt=issued_at, status=row.status
    )


def _snapshot(row) -> CertificateRead:
    if not row.snapshot_envelope or not row.token_envelope:
        # Legacy issuances cannot be reconstructed without changing their claims.
        raise CertificateUnavailable
    try:
        dto = CertificateRead.model_validate_json(
            crypt(row.snapshot_envelope, row.id, "snapshot", decrypt=True)
        )
        token = crypt(row.token_envelope, row.id, "token", decrypt=True)
        if (
            dto.certificateId != row.id
            or token_hash(token) != row.verification_token_hash
            or not dto.verificationUrl.endswith(f"/verify/{token}")
        ):
            raise CertificateUnavailable
        return dto.model_copy(update={"status": row.status})
    except ValidationError:
        raise CertificateUnavailable from None


async def _assessment(db, student_id, term_id):
    stored = await billing.read_stored_assessment(db, student_id, term_id)
    if stored is None:
        raise CorNotFinal(["NO_ASSESSMENT"])
    return stored


async def read_assessment(db, student_id, term_id):
    if await repository.enrollment(db, student_id, term_id) is None:
        raise CorNotEnrolled
    return (await _assessment(db, student_id, term_id)).assessment


async def read_certificate(db, student_id, term_id):
    enrollment = await repository.enrollment(db, student_id, term_id)
    if enrollment is None:
        raise CorNotEnrolled
    row = await repository.current(db, enrollment.id)
    if row is None:
        try:
            await _preconditions(db, enrollment)
        except CertificateUnavailable:
            # GET can refuse an unissued COR without an available producer.
            raise CorNotFinal() from None
        raise CorNotFinal()
    return _snapshot(row)


async def _preconditions(db, enrollment):
    profile = await users.get_student_profile(db, enrollment.student_id)
    if profile is None:
        raise ResourceNotFoundError
    registrations = await repository.registrations(
        db, enrollment.id, enrollment.academic_term_id
    )
    if len(registrations) > 100:
        raise CertificateUnavailable
    stored = await billing.read_stored_assessment(
        db, enrollment.student_id, enrollment.academic_term_id
    )
    unmet = []
    if profile.has_financial_hold or profile.has_academic_hold:
        unmet.append("HOLD")
    if stored is None or not registrations:
        unmet.append("NO_ASSESSMENT")
    # Surface known refusals even while the producer verdict interface is absent.
    # When available the shared checker supplies the definitive academic result.
    try:
        eligible = await academic_verdict.certificate_eligible(
            db,
            student_id=enrollment.student_id,
            term_id=enrollment.academic_term_id,
            section_ids=[r.section_id for r in registrations],
        )
    except CertificateUnavailable:
        if unmet:
            raise CorNotFinal(unmet) from None
        raise
    if eligible is not True:
        unmet.append("PREREQUISITE_VIOLATION")
    if unmet:
        raise CorNotFinal(unmet)
    if enrollment.enrollment_status not in OFFICIAL | {"ENROLLED_PENDING"}:
        raise CorNotFinal()
    return profile, registrations, stored


async def _content(
    db, enrollment, profile, registrations, stored, certificate_id, issued_at, token
):
    terms = await academics.record_terms(db, [enrollment.academic_term_id])
    identity = await users.schedule_identity(db, enrollment.student_id)
    program = await curriculum.get_program(db, profile.program_id)
    section_rows = await sections.schedule_sections(
        db, [r.section_id for r in registrations]
    )
    courses = {
        c.id: c
        for c in await curriculum.schedule_courses(
            db, [s.course_id for s in section_rows]
        )
    }
    names = await users.schedule_names(
        db, [s.faculty_id for s in section_rows if s.faculty_id]
    )
    if (
        len(terms) != 1
        or not identity
        or not program
        or len(section_rows) != len(registrations)
        or sum(len(s.meetings) for s in section_rows) > 1000
    ):
        raise CertificateUnavailable
    output = []
    for section in section_rows:
        course = courses.get(section.course_id)
        if course is None or section.academic_term_id != enrollment.academic_term_id:
            raise CertificateUnavailable
        if any(m.end_time <= m.start_time for m in section.meetings):
            raise CertificateUnavailable
        output.append(
            CertificateCourse(
                sectionId=section.id,
                code=course.course_code,
                title=course.title,
                section=section.section_name,
                units=course.lecture_units + course.lab_units,
                schedule=[
                    CertificateMeeting(
                        dayOfWeek=m.day_of_week,
                        startTime=m.start_time,
                        endTime=m.end_time,
                        room=m.room,
                        type=m.schedule_type,
                    )
                    for m in section.meetings
                ],
                room=", ".join(sorted({m.room for m in section.meetings if m.room}))
                or None,
                faculty=names.get(section.faculty_id),
            )
        )
    inputs = stored.inputs
    if (
        {c.section_id: c.units for c in inputs.courses}
        != {c.sectionId: c.units for c in output}
        or inputs.program_id != profile.program_id
        or (inputs.fhe_qualified, inputs.fhe_forfeited, inputs.fhe_forfeiture_reason)
        != (profile.fhe_qualified, profile.fhe_forfeited, profile.fhe_forfeiture_reason)
    ):
        raise CorNotFinal(["NO_ASSESSMENT"])
    origin = settings.frontend_url_for("student").rstrip("/")
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise CertificateUnavailable
    term = terms[0]
    return CertificateRead(
        certificateId=certificate_id,
        issuedAt=issued_at,
        status="CURRENT",
        name=identity.full_name,
        studentNumber=identity.student_number,
        program=program.name,
        term=CertificateTerm(
            id=term.id, year=term.academic_year, semester=term.semester
        ),
        courses=sorted(output, key=lambda c: (c.code, c.section, str(c.sectionId))),
        totalUnits=sum((c.units for c in output), Decimal(0)),
        assessment=stored.assessment,
        verificationUrl=f"{origin}/verify/{token}",
    )


async def _create(
    db, enrollment, *, actor_id, client_ip, reissue=False, actor_role="student"
):
    profile, registrations, stored = await _preconditions(db, enrollment)
    certificate_id, issued_at, token = (
        uuid.uuid4(),
        datetime.now(UTC),
        secrets.token_urlsafe(32),
    )
    dto = await _content(
        db, enrollment, profile, registrations, stored, certificate_id, issued_at, token
    )
    row = repository.add(
        db,
        id=certificate_id,
        student_id=enrollment.student_id,
        term_enrollment_id=enrollment.id,
        verification_token_hash=token_hash(token),
        token_envelope=crypt(token, certificate_id, "token"),
        snapshot_envelope=crypt(dto.model_dump_json(), certificate_id, "snapshot"),
        status="CURRENT",
        issued_at=issued_at,
    )
    old = enrollment.enrollment_status
    enrollment.enrollment_status = "OFFICIALLY_ENROLLED"
    audit.stage(
        db,
        action="enrollment.cor_reissued" if reissue else "enrollment.cor_issued",
        actor_id=actor_id,
        target_type="term_enrollment",
        target_id=enrollment.id,
        prior_state={"status": old},
        new_state={"status": enrollment.enrollment_status},
        payload={
            "actor_role": actor_role,
            "client_ip": client_ip,
        },
        occurred_at=issued_at,
    )
    await db.flush()
    return _metadata(row)


@logged("enrollment.issue_certificate", fields=("student_id", "term_id"))
async def issue_certificate(db, student_id, term_id, *, client_ip=None):
    """HTTP boundary owns one commit; lock parent first for race-safe retries."""
    # Finish only authentication/IAM reads before the business transaction.
    # Internal callers must use stage_reissue, never this HTTP boundary.
    await db.commit()
    for attempt in range(3):
        try:
            await repository.begin_certificate_snapshot(db)
            enrollment = await repository.enrollment(db, student_id, term_id, lock=True)
            if enrollment is None:
                raise CorNotEnrolled
            existing = await repository.current(db, enrollment.id, lock=True)
            if existing:
                _snapshot(existing)  # Never reconstruct legacy content.
                result = _metadata(existing)
            else:
                result = await _create(
                    db, enrollment, actor_id=student_id, client_ip=client_ip
                )
            await db.commit()
            return result
        except DBAPIError as exc:
            await db.rollback()
            state = getattr(exc.orig, "sqlstate", None)
            constraint = getattr(
                getattr(exc.orig, "__cause__", None), "constraint_name", None
            )
            # An already-official legacy parent need not be updated by issue;
            # its competing snapshot can see the unique-index race, not 40001.
            retryable = state in {"40001", "40P01"} or (
                state == "23505" and constraint == "uq_cor_current_enrollment"
            )
            if not retryable or attempt == 2:
                # Do not send SQL parameter records to the global error logger.
                raise CertificateUnavailable from None
        except BaseException:
            await db.rollback()
            raise


async def stage_reissue(
    db,
    *,
    student_id,
    term_id,
    actor_id,
    assessment: StoredAssessment,
    client_ip=None,
    actor_role="admin",
):
    """#101 calls inside its approval transaction. Never commit or roll back here.

    Apply changes and stage_assessment first, holding the parent enrollment lock.
    Any exception MUST roll back the caller's entire approval transaction.
    """
    if not await repository.certificate_snapshot_is_consistent(db):
        raise CertificateUnavailable
    enrollment = await repository.enrollment(db, student_id, term_id, lock=True)
    if enrollment is None:
        raise CorNotEnrolled
    old = await repository.current(db, enrollment.id, lock=True)
    if old is None or enrollment.enrollment_status not in OFFICIAL:
        raise CorNotFinal()
    stored = await _assessment(db, student_id, term_id)
    if assessment != stored:
        raise CorNotFinal(["NO_ASSESSMENT"])
    old.status, old.superseded_at = "SUPERSEDED", datetime.now(UTC)
    await db.flush()  # Release partial-unique slot before creating its successor.
    return await _create(
        db,
        enrollment,
        actor_id=actor_id,
        client_ip=client_ip,
        reissue=True,
        actor_role=actor_role,
    )


async def verify_certificate(db, token):
    if not TOKEN.fullmatch(token):
        raise ResourceNotFoundError
    row = await repository.by_hash(db, token_hash(token))
    if row is None:
        raise ResourceNotFoundError
    dto = _snapshot(row)
    return VerificationRead(
        name=dto.name,
        studentNumber=dto.studentNumber,
        term=VerificationTerm(year=dto.term.year, semester=dto.term.semester),
        issuedAt=dto.issuedAt,
        status=dto.status,
    )
