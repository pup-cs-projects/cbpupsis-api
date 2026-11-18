"""Grade, standing, and transcript reads, without workflow mutations."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.events import Event
from cbpupsis_core.exceptions import ValidationError
from cbpupsis_shared.domains.academics import client as academics
from cbpupsis_shared.domains.audit import client as audit
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.curriculum import client as curriculum
from cbpupsis_shared.domains.enrollment import client as enrollment
from cbpupsis_shared.domains.grading import repository
from cbpupsis_shared.domains.grading.constants import (
    ENROLLED_COURSE_STATUSES,
    GRADE_DRAFT_SAVED_ACTION,
    GRADE_STATE_DRAFT,
    GRADE_STATE_FINAL,
    GRADES_FINALIZED_ACTION,
    GRADES_FINALIZED_EVENT,
    INC_EXPIRY_DAYS,
    NUMERIC_MARKS,
    UNIT_REDUCTIONS,
    AcademicStanding,
)
from cbpupsis_shared.domains.grading.exceptions import (
    GradeDeadlinePassedError,
    GradeRecordUnavailableError,
    GradesLockedError,
    GradeValidationError,
    GradeVersionConflictError,
)
from cbpupsis_shared.domains.grading.schemas import (
    AcademicAttempt,
    AcademicRecord,
    FinalizeGradesRead,
    FinalizeGradesRequest,
    GradeCourse,
    GradeDeadlineRead,
    GradesRead,
    SaveGradeDraftRequest,
    ScholasticStandingRead,
    SectionGradeEntryRead,
    SectionGradesRead,
    SectionRosterGrade,
    TranscriptRead,
    TranscriptTerm,
)
from cbpupsis_shared.domains.sections import client as sections
from cbpupsis_shared.domains.users import client as users
from cbpupsis_shared.outbox import publish_transactional

STANDING_TIERS: dict[str, tuple[int, str | None, bool]] = {
    "Good Standing": (0, None, False),
    "Warning Level 1": (0, "Academic warning level 1", True),
    "Warning Level 2": (3, "Academic warning level 2 with 3 units reduction", True),
    "Probation": (6, "Academic probation with 6 units reduction", True),
    "College Dropping": (0, "College dropping", True),
    "University Dismissal": (0, "University dismissal", True),
}


async def get_student_scholastic_standing(
    db: AsyncSession, student_id: uuid.UUID
) -> ScholasticStandingRead:
    """Return student's scholastic standing adhering strictly to Handbook tiers.

    Uses Handbook Title 5 Section 12 tiers.
    """
    row = await repository.get_latest_student_term_gpa(db, student_id)
    raw_val = (
        row.academic_standing.strip()
        if row and row.academic_standing
        else "Good Standing"
    )

    # Enforce Audit 2.3: 'Suspension' is a Title 9 disciplinary sanction
    if "SUSPENSION" in raw_val.upper():
        raw_val = "Probation"

    matched = None
    for tier in STANDING_TIERS:
        if tier.lower() == raw_val.lower():
            matched = tier
            break

    if not matched:
        matched = "Good Standing"

    reduction, explanation, appeal = STANDING_TIERS[matched]
    return ScholasticStandingRead(
        value=matched,
        unit_reduction=reduction,
        explanation=explanation,
        appeal_available=appeal,
    )


async def get_previous_regular_term_gwa(
    db: AsyncSession, *, student_id: uuid.UUID, before: date
) -> Decimal | None:
    evaluations = await repository.list_student_term_gpas(db, student_id=student_id)
    terms = {
        term.id: term
        for term in await academics.record_terms(
            db, {row.academic_term_id for row in evaluations}
        )
    }
    eligible = [
        row
        for row in evaluations
        if (term := terms.get(row.academic_term_id)) is not None
        and term.end_date < before
        and "summer" not in term.semester.lower()
    ]
    if not eligible:
        return None
    latest = max(
        eligible,
        key=lambda row: (
            terms[row.academic_term_id].end_date,
            row.calculated_at,
            str(row.id),
        ),
    )
    return None if latest.term_gpa is None else Decimal(str(latest.term_gpa))


def utc(value: datetime) -> datetime:
    """SQLite loses timezone information; PostgreSQL stores aware instants."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def weighted_totals(courses: list[GradeCourse]) -> tuple[Decimal, Decimal]:
    numerator, denominator = Decimal(0), Decimal(0)
    for row in courses:
        if row.final_grade not in NUMERIC_MARKS or re.match(
            r"^NSTP(?:\b|[_\d])", row.code.strip(), re.IGNORECASE
        ):
            continue
        numerator += Decimal(row.final_grade) * row.units
        denominator += row.units
    return numerator, denominator


def average(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if not denominator:
        return None
    return (numerator / denominator).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def earned_courses(courses: list[GradeCourse]) -> dict[uuid.UUID, Decimal]:
    return {row.course_id: row.units for row in courses if row.status == "Passed"}


def _course(mark, section, course) -> GradeCourse:
    value = mark.final_grade.strip().upper()
    if value not in {"INC", "W"}:
        try:
            numeric = Decimal(value)
            if not numeric.is_finite():
                raise GradeRecordUnavailableError
            value = format(numeric, ".2f")
            if numeric != Decimal(value) or value not in NUMERIC_MARKS:
                raise GradeRecordUnavailableError
        except InvalidOperation as exc:
            raise GradeRecordUnavailableError from exc
    if not course.total_units.is_finite() or course.total_units < 0:
        raise GradeRecordUnavailableError
    if value == "INC" and mark.inc_expires_at is None:
        raise GradeRecordUnavailableError
    status = (
        "Incomplete"
        if value == "INC"
        else "Withdrawn"
        if value == "W"
        else "Failed"
        if value == "5.00"
        else "Passed"
    )
    return GradeCourse(
        course_id=section.course_id,
        enrollment_id=mark.enrollment_item_id,
        code=course.course_code,
        title=course.title,
        units=course.total_units,
        final_grade="Inc" if value == "INC" else value,
        status=status,
        posted_at=utc(mark.submitted_at),
        completion_deadline=utc(mark.inc_expires_at) if value == "INC" else None,
    )


async def _load(
    db: AsyncSession,
    student_id: uuid.UUID,
    *,
    student_number: str | None = None,
    latest: bool = False,
    year: str | None = None,
    semester: str | None = None,
    load_identity: bool = True,
):
    # Staff-owned numeric updates do not depend on the student's login status.
    # Public student reads retain the original active-owner identity gate.
    identity = await users.record_identity(db, student_id) if load_identity else None
    if load_identity and (
        identity is None
        or (student_number is not None and student_number != identity.student_number)
    ):
        raise ResourceNotFoundError
    if (year is None) != (semester is None) or (
        year is not None and (not year.strip() or not semester.strip())
    ):
        raise ValidationError("Supply nonblank year and semester together.")
    enrolled = await enrollment.record_enrollments(db, student_id)
    term_ids = {row.academic_term_id for row in enrolled}
    terms = await academics.record_terms(db, term_ids)
    if len(terms) != len(term_ids):
        raise GradeRecordUnavailableError
    terms.sort(
        key=lambda row: (row.start_date, row.end_date, str(row.id)), reverse=True
    )
    if year is not None:
        terms = [t for t in terms if t.academic_year == year and t.semester == semester]
    elif latest:
        published = await repository.posted_enrollment_ids(
            db, [row.id for row in enrolled]
        )
        marked_terms = {r.academic_term_id for r in enrolled if r.id in published}
        terms = [t for t in terms if t.id in marked_terms][:1]
    wanted = {t.id for t in terms}
    attempts = {r.id: r for r in enrolled if r.academic_term_id in wanted}
    marks = await repository.published_marks(db, list(attempts))
    section_rows = {
        r.id: r
        for r in await sections.record_sections(
            db, {attempts[m.enrollment_item_id].section_id for m in marks}
        )
    }
    catalog = {
        r.id: r
        for r in await curriculum.record_courses(
            db, {r.course_id for r in section_rows.values()}
        )
    }
    by_term = {t.id: [] for t in terms}
    for mark in marks:
        attempt = attempts[mark.enrollment_item_id]
        section = section_rows.get(attempt.section_id)
        course = catalog.get(section.course_id) if section else None
        if (
            section is None
            or course is None
            or section.academic_term_id != attempt.academic_term_id
        ):
            raise GradeRecordUnavailableError
        by_term[attempt.academic_term_id].append(_course(mark, section, course))
    for rows in by_term.values():
        rows.sort(key=lambda row: (row.code, str(row.enrollment_id)))
    return identity, terms, by_term


async def read_grades(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    student_number: str | None = None,
    year: str | None = None,
    semester: str | None = None,
) -> GradesRead:
    identity, terms, by_term = await _load(
        db,
        student_id,
        student_number=student_number,
        latest=True,
        year=year,
        semester=semester,
    )
    result = GradesRead(student=identity)
    if not terms:
        return result
    result.term = term = terms[0]
    result.courses = rows = by_term[term.id]
    result.gwa = average(*weighted_totals(rows))
    result.units_earned = sum(earned_courses(rows).values(), Decimal("0.0"))
    if rows:
        result.state, result.message = "available", None
    standing = await repository.evaluated_standing(db, student_id, term.id)
    if standing is not None:
        try:
            result.academic_standing = AcademicStanding(standing.academic_standing)
        except ValueError as exc:
            raise GradeRecordUnavailableError from exc
        result.unit_reduction = UNIT_REDUCTIONS.get(result.academic_standing)
        result.standing_state, result.standing_message = "evaluated", None
        result.standing_evaluated_at = utc(standing.calculated_at)
    return result


async def read_transcript(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    page: int = 1,
    limit: int = 20,
    complete: bool = False,
) -> TranscriptRead:
    if page < 1 or not 1 <= limit <= 100:
        raise ValidationError(
            "Page must be positive and limit must be between 1 and 100."
        )
    identity, terms, by_term = await _load(db, student_id)
    result = TranscriptRead(student=identity, page=page, limit=limit, total=0)
    numerator, denominator = Decimal(0), Decimal(0)
    earned, blocks = {}, []
    today = datetime.now(ZoneInfo("Asia/Manila")).date()
    for term in reversed(terms):
        rows = by_term[term.id]
        if not rows:
            continue
        term_numerator, term_denominator = weighted_totals(rows)
        completed = term.end_date < today
        if completed:
            numerator += term_numerator
            denominator += term_denominator
        for course_id, units in earned_courses(rows).items():
            earned.setdefault(course_id, units)
        blocks.append(
            TranscriptTerm(
                term=term,
                courses=rows,
                gwa=average(term_numerator, term_denominator),
                units_earned=sum(earned_courses(rows).values(), Decimal("0.0")),
                cumulative_gwa=average(numerator, denominator),
                total_units_earned=sum(earned.values(), Decimal("0.0")),
                completed=completed,
            )
        )
    blocks.reverse()
    result.total = len(blocks)
    result.terms = blocks if complete else blocks[(page - 1) * limit : page * limit]
    result.next_page = page + 1 if not complete and page * limit < len(blocks) else None
    result.cumulative_gwa = average(numerator, denominator)
    result.total_units_earned = sum(earned.values(), Decimal("0.0"))
    if blocks:
        result.state, result.message = "available", None
    return result


async def academic_record(db: AsyncSession, student_id: uuid.UUID) -> AcademicRecord:
    """Use the canonical published transcript calculation, never stored defaults."""
    transcript = await read_transcript(db, student_id=student_id, complete=True)
    return AcademicRecord(
        attempts=[
            AcademicAttempt(
                enrollment_item_id=c.enrollment_id,
                course_id=c.course_id,
                mark=c.final_grade,
            )
            for term in transcript.terms
            for c in term.courses
        ],
        cumulative_gwa=transcript.cumulative_gwa,
    )


async def section_roster_grades(
    db: AsyncSession, enrollment_ids: set[uuid.UUID]
) -> dict[uuid.UUID, SectionRosterGrade]:
    result = {
        enrollment_id: SectionRosterGrade(
            enrollment_id=enrollment_id, grade_status="PENDING"
        )
        for enrollment_id in enrollment_ids
    }
    for row in await repository.roster_grades(db, enrollment_ids):
        final_grade = (row.final_grade or "").strip().upper()
        status = (
            "INC"
            if not row.is_draft and final_grade == "INC"
            else "DRAFT"
            if row.is_draft
            else "SUBMITTED"
            if row.is_locked or row.submitted_at is not None
            else "PENDING"
        )
        result[row.enrollment_item_id] = SectionRosterGrade(
            enrollment_id=row.enrollment_item_id, grade_status=status
        )
    return result


def _utc8_iso(value: datetime) -> str:
    return utc(value).astimezone(timezone(timedelta(hours=8))).isoformat()


def _is_enrolled(status: str) -> bool:
    return status.strip().lower() in ENROLLED_COURSE_STATUSES


def _normalize_mark(value: object) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError(
            "Mark must be between 1.00 and 5.00 on the grading scale, or INC."
        )
    raw = str(value).strip()
    if not raw:
        raise ValueError("Mark must not be blank.")
    if raw.upper() == "INC":
        return "INC"
    try:
        numeric = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(
            "Mark must be between 1.00 and 5.00 on the grading scale, or INC."
        ) from exc
    if not numeric.is_finite():
        raise ValueError("Mark must be finite.")
    # Bound magnitude before fixed-point formatting can allocate exponent-sized text.
    if not Decimal("1.00") <= numeric <= Decimal("5.00"):
        raise ValueError(
            "Mark must be a valid grade (1.00 to 3.00 in 0.25 steps, 5.00, or INC)."
        )
    formatted = format(numeric, ".2f")
    if numeric != Decimal(formatted) or formatted not in NUMERIC_MARKS:
        raise ValueError(
            "Mark must be a valid grade (1.00 to 3.00 in 0.25 steps, 5.00, or INC)."
        )
    return formatted


def _normalize_raw_score(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Raw score must be between 0 and 100.")
    raw = str(value).strip()
    if not raw:
        raise ValueError("Raw score must not be blank.")
    try:
        numeric = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Raw score must be a valid number.") from exc
    if not numeric.is_finite() or numeric < Decimal("0") or numeric > Decimal("100"):
        raise ValueError("Raw score must be between 0 and 100.")
    return numeric.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _infer_section_state(grades: list) -> str:
    if grades and all(g.is_locked and not g.is_draft for g in grades):
        return GRADE_STATE_FINAL
    return GRADE_STATE_DRAFT


def _grade_snapshot(grade, student_id: uuid.UUID) -> dict[str, object]:
    return {
        "studentId": str(student_id),
        "finalGrade": grade.final_grade if grade else None,
        "rawScore": (
            str(grade.raw_score)
            if grade is not None and grade.raw_score is not None
            else None
        ),
        "midtermGrade": grade.midterm_grade if grade else None,
        "remarks": grade.remarks if grade else None,
    }


async def _load_section_context(
    db: AsyncSession, *, faculty_id: uuid.UUID, section_id: uuid.UUID
):
    section = await sections.get_owned_section(
        db, faculty_id=faculty_id, section_id=section_id
    )
    term = await academics.get_academic_term(db, section.academic_term_id)
    if term is None or term.grade_submission_deadline is None:
        raise GradeRecordUnavailableError
    courses = await curriculum.record_courses(db, {section.course_id})
    course = courses[0] if courses else None
    memberships = [
        row
        for row in await enrollment.section_roster_enrollments(db, section_id)
        if _is_enrolled(row.enrollment_status)
    ]
    identities = await users.faculty_roster_identities(
        db, {row.student_id for row in memberships}
    )
    memberships.sort(
        key=lambda row: (
            identities[row.student_id].name.casefold()
            if row.student_id in identities
            else "",
            identities[row.student_id].student_number
            if row.student_id in identities
            else "",
            str(row.student_id),
        )
    )
    return section, term, course, memberships, identities


async def _ensure_locked_submission(
    db: AsyncSession, *, section_id: uuid.UUID, initial_state: str, now: datetime
):
    submission = await repository.get_section_submission(db, section_id, lock=True)
    if submission is not None:
        return submission
    try:
        async with db.begin_nested():
            submission = repository.add_section_submission(
                db,
                section_id=section_id,
                state=initial_state,
                version=1,
                updated_at=now,
            )
            await db.flush()
            return submission
    except IntegrityError:
        submission = await repository.get_section_submission(db, section_id, lock=True)
        if submission is None:  # pragma: no cover - unreachable after IntegrityError
            raise GradeRecordUnavailableError from None
        return submission


def _build_section_grades_read(
    *,
    section,
    term,
    course,
    memberships,
    identities,
    grades_by_enrollment: dict[uuid.UUID, object],
    state: str,
    version: int,
    submitted_at: datetime | None,
    now: datetime,
) -> SectionGradesRead:
    deadline = utc(term.grade_submission_deadline)
    entries: list[SectionGradeEntryRead] = []
    for membership in memberships:
        identity = identities.get(membership.student_id)
        student_number = identity.student_number if identity else ""
        student_name = identity.name if identity else ""
        grade = grades_by_enrollment.get(membership.enrollment_id)
        is_draft = True if grade is None else bool(grade.is_draft)
        is_locked = False if grade is None else bool(grade.is_locked)
        final_grade = None if grade is None else grade.final_grade
        if is_locked and not is_draft:
            row_status = "FINAL"
        elif final_grade is not None:
            row_status = "DRAFT"
        else:
            row_status = "MISSING"
        entries.append(
            SectionGradeEntryRead(
                enrollment_id=membership.enrollment_id,
                student_id=membership.student_id,
                student_number=student_number,
                student_name=student_name,
                raw_score=None if grade is None else grade.raw_score,
                midterm_grade=None if grade is None else grade.midterm_grade,
                final_grade=final_grade,
                remarks=None if grade is None else grade.remarks,
                inc_expires_at=(
                    utc(grade.inc_expires_at)
                    if grade is not None and grade.inc_expires_at is not None
                    else None
                ),
                is_draft=is_draft,
                is_locked=is_locked,
                status=row_status,
                submitted_at=(
                    utc(grade.submitted_at)
                    if grade is not None and grade.submitted_at is not None
                    else None
                ),
            )
        )
    return SectionGradesRead(
        section_id=section.id,
        section_name=section.section_name,
        course_code=course.course_code if course else None,
        course_title=course.title if course else None,
        state="FINAL" if state == GRADE_STATE_FINAL else "DRAFT",
        version=version,
        deadline=deadline,
        deadline_passed=now > deadline,
        submitted_at=utc(submitted_at) if submitted_at is not None else None,
        grades=entries,
    )


async def get_section_grades(
    db: AsyncSession, *, faculty_id: uuid.UUID, section_id: uuid.UUID
) -> SectionGradesRead:
    """Return enrolled students' marks, state, version, and deadline."""
    section, term, course, memberships, identities = await _load_section_context(
        db, faculty_id=faculty_id, section_id=section_id
    )
    grades = await repository.roster_grades(db, {m.enrollment_id for m in memberships})
    grades_by_enrollment = {g.enrollment_item_id: g for g in grades}
    submission = await repository.get_section_submission(db, section_id)
    if submission is not None:
        state = submission.state
        version = submission.version
        submitted_at = submission.finalized_at
    else:
        state = _infer_section_state(grades)
        version = 1
        submitted_at = (
            max((g.submitted_at for g in grades if g.submitted_at), default=None)
            if state == GRADE_STATE_FINAL
            else None
        )
    return _build_section_grades_read(
        section=section,
        term=term,
        course=course,
        memberships=memberships,
        identities=identities,
        grades_by_enrollment=grades_by_enrollment,
        state=state,
        version=version,
        submitted_at=submitted_at,
        now=datetime.now(UTC),
    )


async def get_section_grade_deadline(
    db: AsyncSession, *, faculty_id: uuid.UUID, section_id: uuid.UUID
) -> GradeDeadlineRead:
    """Return the section's grade submission deadline and whether it has passed."""
    section = await sections.get_owned_section(
        db, faculty_id=faculty_id, section_id=section_id
    )
    term = await academics.get_academic_term(db, section.academic_term_id)
    if term is None or term.grade_submission_deadline is None:
        raise GradeRecordUnavailableError
    deadline = utc(term.grade_submission_deadline)
    return GradeDeadlineRead(
        section_id=section.id,
        academic_term_id=section.academic_term_id,
        deadline=deadline,
        passed=datetime.now(UTC) > deadline,
    )


def _resolve_target_membership(
    item,
    *,
    row_index: int,
    by_enrollment: dict[uuid.UUID, object],
    by_student: dict[uuid.UUID, object],
) -> tuple[object | None, dict[str, object] | None]:
    if item.enrollment_id is None and item.student_id is None:
        return None, {
            "rowIndex": row_index,
            "field": "enrollmentId",
            "message": "Provide enrollmentId or studentId for each student row.",
        }
    target = None
    if item.enrollment_id is not None:
        target = by_enrollment.get(item.enrollment_id)
        if target is None or (
            item.student_id is not None and target.student_id != item.student_id
        ):
            return None, {
                "rowIndex": row_index,
                "enrollmentId": str(item.enrollment_id),
                "studentId": str(item.student_id) if item.student_id else None,
                "field": "enrollmentId",
                "message": "Student enrollment does not belong to this section.",
            }
    elif item.student_id is not None:
        target = by_student.get(item.student_id)
        if target is None:
            return None, {
                "rowIndex": row_index,
                "studentId": str(item.student_id),
                "field": "studentId",
                "message": "Student is not enrolled in this section.",
            }
    return target, None


async def save_section_grade_draft(
    db: AsyncSession,
    *,
    faculty_id: uuid.UUID,
    section_id: uuid.UUID,
    payload: SaveGradeDraftRequest,
    client_ip: str | None = None,
) -> SectionGradesRead:
    """Validate and save draft marks with optimistic locking and audit logging."""
    section, term, course, memberships, identities = await _load_section_context(
        db, faculty_id=faculty_id, section_id=section_id
    )
    by_enrollment = {m.enrollment_id: m for m in memberships}
    by_student = {m.student_id: m for m in memberships}
    now = datetime.now(UTC)

    try:
        existing_grades = await repository.roster_grades_for_update(
            db, set(by_enrollment)
        )
        submission = await _ensure_locked_submission(
            db,
            section_id=section_id,
            initial_state=_infer_section_state(existing_grades),
            now=now,
        )
        if submission.state == GRADE_STATE_FINAL or any(
            g.is_locked and not g.is_draft for g in existing_grades
        ):
            raise GradesLockedError
        if submission.version != payload.version:
            raise GradeVersionConflictError(current_version=submission.version)

        row_errors: list[dict[str, object]] = []
        seen_enrollments: set[uuid.UUID] = set()
        normalized_items: list[tuple[object, object, dict[str, object]]] = []

        for idx, item in enumerate(payload.grades):
            target, target_error = _resolve_target_membership(
                item,
                row_index=idx,
                by_enrollment=by_enrollment,
                by_student=by_student,
            )
            if target_error is not None or target is None:
                row_errors.append(target_error)  # type: ignore[arg-type]
                continue
            identity = identities.get(target.student_id)
            row_meta = {
                "rowIndex": idx,
                "enrollmentId": str(target.enrollment_id),
                "studentId": str(target.student_id),
                "studentNumber": identity.student_number if identity else None,
                "studentName": identity.name if identity else None,
            }
            if target.enrollment_id in seen_enrollments:
                row_errors.append(
                    {
                        **row_meta,
                        "field": "enrollmentId",
                        "message": "Duplicate student row in draft payload.",
                    }
                )
                continue
            seen_enrollments.add(target.enrollment_id)

            updates: dict[str, object] = {}
            if "final_grade" in item.model_fields_set:
                if item.final_grade is None:
                    updates["final_grade"] = None
                else:
                    try:
                        updates["final_grade"] = _normalize_mark(item.final_grade)
                    except ValueError as exc:
                        row_errors.append(
                            {
                                **row_meta,
                                "field": "finalGrade",
                                "value": str(item.final_grade),
                                "message": str(exc),
                            }
                        )
                        continue
            if "raw_score" in item.model_fields_set:
                try:
                    updates["raw_score"] = _normalize_raw_score(item.raw_score)
                except ValueError as exc:
                    row_errors.append(
                        {
                            **row_meta,
                            "field": "rawScore",
                            "value": str(item.raw_score),
                            "message": str(exc),
                        }
                    )
                    continue
            if "midterm_grade" in item.model_fields_set:
                updates["midterm_grade"] = (
                    item.midterm_grade.strip()
                    if item.midterm_grade is not None
                    else None
                )
            if "remarks" in item.model_fields_set:
                updates["remarks"] = (
                    item.remarks.strip() if item.remarks is not None else None
                )
            normalized_items.append((target, item, updates))

        if row_errors:
            raise GradeValidationError(row_errors)

        grades_by_enrollment = {g.enrollment_item_id: g for g in existing_grades}
        prior_marks = {
            str(m.enrollment_id): _grade_snapshot(
                grades_by_enrollment.get(m.enrollment_id), m.student_id
            )
            for m in memberships
        }
        prior_version = submission.version
        prior_state_name = submission.state

        for target, _item, updates in normalized_items:
            row = grades_by_enrollment.get(target.enrollment_id)
            if row is None:
                row = repository.add_student_grade(
                    db,
                    enrollment_item_id=target.enrollment_id,
                    raw_score=updates.get("raw_score"),  # type: ignore[arg-type]
                    midterm_grade=updates.get("midterm_grade"),  # type: ignore[arg-type]
                    final_grade=updates.get("final_grade"),  # type: ignore[arg-type]
                    remarks=updates.get("remarks"),  # type: ignore[arg-type]
                    is_draft=True,
                    is_locked=False,
                    updated_at=now,
                )
                grades_by_enrollment[target.enrollment_id] = row
            else:
                for key, val in updates.items():
                    setattr(row, key, val)
                row.is_draft = True
                row.is_locked = False
                row.inc_expires_at = None
                row.updated_at = now

        submission.state = GRADE_STATE_DRAFT
        submission.version += 1
        submission.updated_at = now
        await db.flush()

        new_marks = {
            str(m.enrollment_id): _grade_snapshot(
                grades_by_enrollment.get(m.enrollment_id), m.student_id
            )
            for m in memberships
        }
        audit.stage(
            db,
            action=GRADE_DRAFT_SAVED_ACTION,
            actor_id=faculty_id,
            target_type="course_section",
            target_id=section.id,
            occurred_at=now,
            prior_state={
                "state": prior_state_name,
                "version": prior_version,
                "marks": prior_marks,
            },
            new_state={
                "state": submission.state,
                "version": submission.version,
                "marks": new_marks,
            },
            payload={
                "sectionId": str(section.id),
                "actorId": str(faculty_id),
                "role": "faculty",
                "ip_address": client_ip,
                "clientIp": client_ip,
                "timestampUtc8": _utc8_iso(now),
                "recordsUpdated": len(normalized_items),
            },
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return _build_section_grades_read(
        section=section,
        term=term,
        course=course,
        memberships=memberships,
        identities=identities,
        grades_by_enrollment=grades_by_enrollment,
        state=submission.state,
        version=submission.version,
        submitted_at=submission.finalized_at,
        now=now,
    )


async def finalize_section_grades(
    db: AsyncSession,
    *,
    faculty_id: uuid.UUID,
    section_id: uuid.UUID,
    payload: FinalizeGradesRequest | None = None,
    client_ip: str | None = None,
) -> FinalizeGradesRead:
    """Lock all enrolled grades atomically, audit, and queue student notifications."""
    section, term, course, memberships, identities = await _load_section_context(
        db, faculty_id=faculty_id, section_id=section_id
    )
    deadline = utc(term.grade_submission_deadline)
    now = datetime.now(UTC)
    enrollment_ids = {m.enrollment_id for m in memberships}

    try:
        existing_grades = await repository.roster_grades_for_update(db, enrollment_ids)
        submission = await _ensure_locked_submission(
            db,
            section_id=section_id,
            initial_state=_infer_section_state(existing_grades),
            now=now,
        )
        if submission.state == GRADE_STATE_FINAL or any(
            g.is_locked and not g.is_draft for g in existing_grades
        ):
            raise GradesLockedError
        if (
            payload is not None
            and payload.version is not None
            and submission.version != payload.version
        ):
            raise GradeVersionConflictError(current_version=submission.version)
        if now > deadline:
            raise GradeDeadlinePassedError(deadline=deadline)

        grades_by_enrollment = {g.enrollment_item_id: g for g in existing_grades}
        row_errors: list[dict[str, object]] = []
        normalized_final_marks: dict[uuid.UUID, str] = {}
        for idx, membership in enumerate(memberships):
            identity = identities.get(membership.student_id)
            row_meta = {
                "rowIndex": idx,
                "enrollmentId": str(membership.enrollment_id),
                "studentId": str(membership.student_id),
                "studentNumber": identity.student_number if identity else None,
                "studentName": identity.name if identity else None,
            }
            grade = grades_by_enrollment.get(membership.enrollment_id)
            if grade is None or grade.final_grade is None:
                row_errors.append(
                    {
                        **row_meta,
                        "field": "finalGrade",
                        "message": "Final mark is required before finalizing.",
                    }
                )
                continue
            try:
                normalized_final_marks[membership.enrollment_id] = _normalize_mark(
                    grade.final_grade
                )
            except ValueError as exc:
                row_errors.append(
                    {
                        **row_meta,
                        "field": "finalGrade",
                        "value": str(grade.final_grade),
                        "message": str(exc),
                    }
                )
        if row_errors:
            raise GradeValidationError(row_errors)

        prior_marks = {
            str(m.enrollment_id): _grade_snapshot(
                grades_by_enrollment.get(m.enrollment_id), m.student_id
            )
            for m in memberships
        }
        prior_version = submission.version
        prior_state_name = submission.state

        inc_deadline = now + timedelta(days=INC_EXPIRY_DAYS)
        for membership in memberships:
            row = grades_by_enrollment[membership.enrollment_id]
            mark = normalized_final_marks[membership.enrollment_id]
            row.final_grade = mark
            row.inc_expires_at = inc_deadline if mark == "INC" else None
            row.is_draft = False
            row.is_locked = True
            row.submitted_by = faculty_id
            row.submitted_at = now
            row.updated_at = now

        submission.state = GRADE_STATE_FINAL
        submission.version += 1
        submission.finalized_by = faculty_id
        submission.finalized_at = now
        submission.updated_at = now
        await db.flush()

        new_marks = {
            str(m.enrollment_id): _grade_snapshot(
                grades_by_enrollment.get(m.enrollment_id), m.student_id
            )
            for m in memberships
        }
        audit.stage(
            db,
            action=GRADES_FINALIZED_ACTION,
            actor_id=faculty_id,
            target_type="course_section",
            target_id=section.id,
            occurred_at=now,
            prior_state={
                "state": prior_state_name,
                "version": prior_version,
                "marks": prior_marks,
            },
            new_state={
                "state": submission.state,
                "version": submission.version,
                "marks": new_marks,
            },
            payload={
                "sectionId": str(section.id),
                "actorId": str(faculty_id),
                "role": "faculty",
                "ip_address": client_ip,
                "clientIp": client_ip,
                "timestampUtc8": _utc8_iso(now),
                "recordsCommitted": len(memberships),
            },
        )

        for membership in memberships:
            publish_transactional(
                db,
                Event(
                    name=GRADES_FINALIZED_EVENT,
                    payload={
                        "user_id": str(membership.student_id),
                        "section_id": str(section.id),
                        "enrollment_id": str(membership.enrollment_id),
                        "course_code": course.course_code if course else None,
                        "section_name": section.section_name,
                        "finalized_at": now.isoformat(),
                    },
                ),
            )

        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return FinalizeGradesRead(
        section_id=section.id,
        state="FINAL",
        version=submission.version,
        records_committed=len(memberships),
        notifications_queued=len(memberships),
        submitted_at=now,
    )
