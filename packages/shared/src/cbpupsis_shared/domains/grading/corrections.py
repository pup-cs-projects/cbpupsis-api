"""Correction of Entry: owner-scoped reads and atomic, ordered decisions."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_core.exceptions import ValidationError
from cbpupsis_shared.domains.academics import client as academics
from cbpupsis_shared.domains.audit import client as audit
from cbpupsis_shared.domains.audit.schemas import render_in_utc8
from cbpupsis_shared.domains.auth.exceptions import (
    InsufficientRoleError,
    ResourceNotFoundError,
)
from cbpupsis_shared.domains.curriculum import client as curriculum
from cbpupsis_shared.domains.enrollment import client as enrollment
from cbpupsis_shared.domains.grading import repository, service
from cbpupsis_shared.domains.grading.attachments import inspect_grading_sheet
from cbpupsis_shared.domains.grading.constants import INC_EXPIRY_DAYS
from cbpupsis_shared.domains.grading.correction_schemas import (
    CorrectionDecision,
    CorrectionPage,
    CorrectionRead,
)
from cbpupsis_shared.domains.grading.exceptions import (
    ApprovalOutOfOrderError,
    ApprovalSameActorError,
    CorrectionWindowClosedError,
    DecisionRemarksRequiredError,
    GradeRecordUnavailableError,
    GradeVersionConflictError,
)
from cbpupsis_shared.domains.sections import client as sections
from cbpupsis_shared.domains.users import client as users
from cbpupsis_shared.domains.users.exceptions import UserNotFoundError

MANILA = ZoneInfo("Asia/Manila")
STEPS = {
    "PENDING_CHAIR": ("chairperson", "ENDORSE", "PENDING_DEAN", "chair"),
    "PENDING_DEAN": ("dean", "ENDORSE", "PENDING_REGISTRAR", "dean"),
    "PENDING_REGISTRAR": ("registrar", "COMMIT", "COMMITTED", "registrar"),
}


def _now() -> datetime:
    return datetime.now(UTC)


async def _lock_student(db, student_id):
    # Login deactivation is not a ban on correcting the student's academic record.
    try:
        await users.lock_role_subject(db, student_id)
    except UserNotFoundError as exc:
        raise ResourceNotFoundError from exc


def _read(row) -> CorrectionRead:
    result = CorrectionRead.model_validate(row)
    # SQLite's timezone loss must not leak naive instants into the HTTP contract.
    for name in (
        "created_at",
        "window_closed_at",
        "resolved_at",
        "chair_approved_at",
        "dean_approved_at",
        "registrar_approved_at",
    ):
        value = getattr(result, name)
        if value is not None:
            setattr(result, name, service.utc(value))
    return result


async def _admin_sections(db, *, position, department_id, college_id):
    if position not in {"chairperson", "dean", "registrar"}:
        raise InsufficientRoleError
    if position == "registrar":
        return None
    if (position == "chairperson" and not department_id) or (
        position == "dean" and not college_id
    ):
        return []
    courses = await curriculum.list_catalog_courses(
        db, position=position, department_id=department_id, college_id=college_id
    )
    return await sections.ids_for_courses(db, [row.id for row in courses])


async def list_corrections(
    db: AsyncSession,
    *,
    faculty_id=None,
    position=None,
    department_id=None,
    college_id=None,
    limit=50,
    offset=0,
) -> CorrectionPage:
    if not 1 <= limit <= 100 or offset < 0:
        raise ValidationError("Invalid pagination.")
    scoped = None
    if faculty_id is None:
        scoped = await _admin_sections(
            db, position=position, department_id=department_id, college_id=college_id
        )
    rows, total = await repository.correction_page(
        db, faculty_id=faculty_id, section_ids=scoped, limit=limit, offset=offset
    )
    return CorrectionPage(
        items=[_read(row) for row in rows], total=total, limit=limit, offset=offset
    )


async def _visible(
    db,
    correction_id,
    *,
    faculty_id=None,
    position=None,
    department_id=None,
    college_id=None,
    lock=False,
):
    scoped = None
    if faculty_id is None:
        scoped = await _admin_sections(
            db, position=position, department_id=department_id, college_id=college_id
        )
    row = await repository.correction(db, correction_id, lock=lock)
    if (
        row is None
        or (faculty_id is not None and row.faculty_id != faculty_id)
        or (scoped is not None and row.section_id not in scoped)
    ):
        raise ResourceNotFoundError
    return row


async def read_correction(db, correction_id, **scope) -> CorrectionRead:
    return _read(await _visible(db, correction_id, **scope))


async def window_closed_at(db, *, section_term, released_at):
    """The inclusive end of N actual regular semesters after publication.

    A semester already running at publication is not the *next* semester.
    Summer/nonregular terms do not consume the configured semester count.
    Missing or ambiguous classification/calendar data fails closed, not in days.
    """
    if section_term.start_date is None:
        raise GradeRecordUnavailableError
    release_date = service.utc(released_at).astimezone(MANILA).date()
    terms = await academics.correction_window_terms(db, section_term.start_date)
    candidates = []
    for term in terms:
        if term.start_date is None or term.end_date is None:
            raise GradeRecordUnavailableError
        if term.start_date <= release_date:
            continue
        if not term.term_type:
            raise GradeRecordUnavailableError
        kind = term.term_type.strip().upper()
        if kind in {"SUMMER", "MIDYEAR", "MID_YEAR", "NON_REGULAR", "NON-REGULAR"}:
            continue
        if kind != "REGULAR" or term.end_date < term.start_date:
            raise GradeRecordUnavailableError
        candidates.append(term)
        if len(candidates) == settings.grade_correction_window_regular_semesters:
            return datetime.combine(term.end_date, time.max, tzinfo=MANILA)
    raise GradeRecordUnavailableError


def _stage_audit(db, row, *, action, actor_id, position, old_state, now, client_ip):
    audit.stage(
        db,
        action=f"grade_correction.{action.lower()}",
        actor_id=actor_id,
        target_type="grade_correction",
        target_id=row.id,
        occurred_at=now,
        prior_state={"state": old_state, "mark": row.current_mark},
        new_state={
            "state": row.state,
            "mark": row.proposed_mark if row.state == "COMMITTED" else row.current_mark,
        },
        payload={
            "actorId": str(actor_id),
            "role": "faculty" if action == "FILE" else "admin",
            "position": position,
            "oldMark": row.current_mark,
            "newMark": row.proposed_mark,
            "timestampUtc8": render_in_utc8(now),
            "clientIp": client_ip,
            "student": "[MASKED]",
            "reason": "[MASKED]",
            "remarks": "[MASKED]",
            "version": row.version,
        },
    )


async def file_correction(
    db: AsyncSession,
    *,
    faculty_id,
    section_id,
    student_id,
    proposed_mark,
    reason,
    content: bytes,
    client_ip=None,
) -> CorrectionRead:
    try:
        # Ownership precedes attachment/record validation to retain the opaque 404.
        section = await sections.get_owned_section(
            db, faculty_id=faculty_id, section_id=section_id
        )
        try:
            mark = service._normalize_mark(proposed_mark)
        except ValueError as exc:
            raise ValidationError(str(exc), code="VALIDATION_ERROR") from exc
        reason = reason.strip()
        if not reason or len(reason) > 4000:
            raise ValidationError(
                "Supply a nonblank reason of at most 4000 characters.",
                code="VALIDATION_ERROR",
            )
        content_type = await asyncio.to_thread(inspect_grading_sheet, content)
        course = await curriculum.get_course_read(db, section.course_id)
        if (
            course is None
            or not (course.department or "").strip()
            or not (course.college or "").strip()
        ):
            # A request must be routable to real department/college approvers.
            raise GradeRecordUnavailableError
        # Serialize all corrections to one student's averages, including first use
        # when there is no cached student_term_gpa row to lock yet.
        await _lock_student(db, student_id)
        attempts = await enrollment.record_enrollments(db, student_id)
        matches = [row for row in attempts if row.section_id == section_id]
        if len(matches) != 1:
            raise ResourceNotFoundError
        grade = await repository.lock_grade(db, enrollment_id=matches[0].id)
        if grade is None:
            raise ResourceNotFoundError
        if (
            grade.is_draft
            or not grade.is_locked
            or not grade.final_grade
            or not grade.submitted_at
        ):
            raise ValidationError(
                "Only a published final grade can be corrected.",
                code="VALIDATION_ERROR",
            )
        term = await academics.get_academic_term(db, section.academic_term_id)
        if term is None:
            raise GradeRecordUnavailableError
        now = _now()
        if service.utc(grade.submitted_at) > now:
            raise GradeRecordUnavailableError
        closed_at = await window_closed_at(
            db, section_term=term, released_at=grade.submitted_at
        )
        if now > closed_at:
            raise CorrectionWindowClosedError(closed_at)
        row = repository.stage_correction(
            db,
            id=uuid.uuid4(),
            grade_id=grade.id,
            student_id=student_id,
            section_id=section_id,
            faculty_id=faculty_id,
            current_mark=grade.final_grade,
            proposed_mark=mark,
            reason=reason,
            grading_sheet_file_id=str(uuid.uuid4()),
            grading_sheet_content=content,
            grading_sheet_content_type=content_type,
            grading_sheet_size=len(content),
            grading_sheet_sha256=hashlib.sha256(content).hexdigest(),
            window_closed_at=service.utc(closed_at),
            grade_updated_at=grade.updated_at,
            state="PENDING_CHAIR",
            version=1,
            created_at=now,
        )
        await db.flush()
        _stage_audit(
            db,
            row,
            action="FILE",
            actor_id=faculty_id,
            position="faculty",
            old_state=None,
            now=now,
            client_ip=client_ip,
        )
        result = _read(row)
        await db.commit()
        return result
    except Exception:
        await db.rollback()
        raise


async def _refresh_gpas(db, student_id, now):
    """Refresh existing cached evaluations using the authoritative read math.

    The reads derive GWA from marks, including when no standing evaluation has
    ever been stored. Do not create a fake Good Standing evaluation here (#105).
    """
    cached = await repository.lock_student_gpas(db, student_id)
    _, terms, by_term = await service._load(db, student_id, load_identity=False)
    cumulative_n, cumulative_d = Decimal(0), Decimal(0)
    totals = {}
    for term in reversed(terms):
        numerator, denominator = service.weighted_totals(by_term[term.id])
        if term.end_date < now.astimezone(MANILA).date():
            cumulative_n += numerator
            cumulative_d += denominator
        totals[term.id] = (
            denominator,
            service.average(numerator, denominator),
            service.average(cumulative_n, cumulative_d),
        )
    for evaluation in cached:
        if evaluation.academic_term_id in totals:
            (
                evaluation.total_gpa_units,
                evaluation.term_gpa,
                evaluation.cumulative_gpa,
            ) = totals[evaluation.academic_term_id]
            # This timestamp is also the standing-evaluation evidence exposed by
            # grade reads; a numeric correction is not a new #105 tier decision.
    await db.flush()


async def decide_correction(
    db: AsyncSession,
    correction_id,
    *,
    actor_id,
    position,
    department_id=None,
    college_id=None,
    decision: CorrectionDecision,
    client_ip=None,
) -> CorrectionRead:
    scope = {
        "position": position,
        "department_id": department_id,
        "college_id": college_id,
    }
    try:
        initial = await _visible(db, correction_id, **scope)
        student_id = initial.student_id
        await _lock_student(db, student_id)
        row = await _visible(db, correction_id, lock=True, **scope)
        if actor_id in {
            row.faculty_id,
            row.chair_approved_by,
            row.dean_approved_by,
            row.registrar_approved_by,
            row.rejected_by,
        }:
            raise ApprovalSameActorError
        if row.version != decision.version:
            raise GradeVersionConflictError(current_version=row.version)
        step = STEPS.get(row.state)
        if (
            step is None
            or position != step[0]
            or decision.action not in {step[1], "REJECT"}
        ):
            raise ApprovalOutOfOrderError
        if decision.action == "REJECT" and not decision.remarks:
            raise DecisionRemarksRequiredError
        old_state, now = row.state, _now()
        if decision.action == "REJECT":
            row.state = "REJECTED"
            row.rejection_remarks = decision.remarks
            row.rejected_by = actor_id
            row.resolved_at = now
        else:
            if decision.action == "COMMIT":
                grade = await repository.lock_grade(db, grade_id=row.grade_id)
                if (
                    grade is None
                    or grade.is_draft
                    or not grade.is_locked
                    or grade.final_grade != row.current_mark
                    or row.grade_updated_at is None
                    or service.utc(grade.updated_at)
                    != service.utc(row.grade_updated_at)
                ):
                    raise GradeVersionConflictError(current_version=row.version)
                grade.final_grade = row.proposed_mark
                grade.updated_at = now
                grade.inc_expires_at = (
                    service.utc(grade.submitted_at) + timedelta(days=INC_EXPIRY_DAYS)
                    if row.proposed_mark == "INC"
                    else None
                )
                grade.completion_status = None
                await db.flush()
                await _refresh_gpas(db, student_id, now)
                row.resolved_at = now
            row.state = step[2]
            setattr(row, f"{step[3]}_approved_by", actor_id)
            setattr(row, f"{step[3]}_approved_at", now)
        row.version += 1
        _stage_audit(
            db,
            row,
            action=decision.action,
            actor_id=actor_id,
            position=position,
            old_state=old_state,
            now=now,
            client_ip=client_ip,
        )
        await db.flush()
        result = _read(row)
        await db.commit()
        return result
    except Exception:
        await db.rollback()
        raise
