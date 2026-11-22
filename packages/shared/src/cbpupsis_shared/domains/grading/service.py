"""Grade, standing, transcript reads, faculty submission, and INC lifecycle."""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Collection
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_core.events import Event
from cbpupsis_core.exceptions import ForbiddenError, ValidationError
from cbpupsis_core.pagination import Page
from cbpupsis_shared.domains.academics import client as academics
from cbpupsis_shared.domains.audit import client as audit
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.curriculum import client as curriculum
from cbpupsis_shared.domains.enrollment import client as enrollment
from cbpupsis_shared.domains.grading import repository
from cbpupsis_shared.domains.grading.constants import (
    COMPLETION_STATE_ENDORSED,
    COMPLETION_STATE_EXPIRED,
    COMPLETION_STATE_PENDING_DEAN,
    COMPLETION_STATE_REJECTED,
    ENROLLED_COURSE_STATUSES,
    GRADE_DRAFT_SAVED_ACTION,
    GRADE_STATE_DRAFT,
    GRADE_STATE_FINAL,
    GRADES_FINALIZED_ACTION,
    GRADES_FINALIZED_EVENT,
    GRADING_MODE_NUMERIC,
    GRADING_MODE_PASS_FAIL,
    INC_COMPLETION_DECIDED_EVENT,
    INC_COMPLETION_ENDORSED_ACTION,
    INC_COMPLETION_FILED_ACTION,
    INC_COMPLETION_REJECTED_ACTION,
    INC_EXPIRED_ACTION,
    INC_EXPIRED_EVENT,
    INC_STATE_COMPLETED,
    INC_STATE_EXPIRED,
    INC_STATE_PENDING,
    NUMERIC_ALLOWED_MARKS,
    NUMERIC_COMPLETION_MARKS,
    NUMERIC_MARKS,
    PASS_FAIL_ALLOWED_MARKS,
    PASS_FAIL_MARKS,
    UNIT_REDUCTIONS,
    AcademicStanding,
)
from cbpupsis_shared.domains.grading.exceptions import (
    GradeDeadlinePassedError,
    GradeModeMismatchError,
    GradeRecordUnavailableError,
    GradesLockedError,
    GradeValidationError,
    GradeVersionConflictError,
    IncAlreadyCompletedError,
    IncCompletionAlreadyDecidedError,
    IncCompletionPendingError,
    IncCompletionWindowClosedError,
    IncompleteDecisionRemarksRequiredError,
    IncompleteValidationError,
)
from cbpupsis_shared.domains.grading.schemas import (
    AcademicAttempt,
    AcademicRecord,
    CreateIncompleteCompletionRequest,
    FinalizeGradesRead,
    FinalizeGradesRequest,
    GradeCourse,
    GradeDeadlineRead,
    GradesRead,
    IncExpiryRunResult,
    IncompleteCompletionDecisionRequest,
    IncompleteCompletionRead,
    IncompleteCompletionSummaryRead,
    IncompleteGradeRead,
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
    if value not in {"INC", "W", "PASS", "FAIL"}:
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
        if value in {"5.00", "FAIL"}
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


def _inc_deadline_for(submitted_at: datetime) -> datetime:
    return utc(submitted_at) + timedelta(days=settings.inc_completion_days)


def _is_enrolled(status: str) -> bool:
    return status.strip().lower() in ENROLLED_COURSE_STATUSES


class _MarkModeMismatch(Exception):
    """Distinguish a grading-mode mismatch from generic mark syntax errors."""


def _section_grading_mode(course) -> Literal["NUMERIC", "PASS_FAIL"]:
    if course is None:
        return GRADING_MODE_NUMERIC
    mode = (course.grading_mode or GRADING_MODE_NUMERIC).strip().upper()
    if mode == GRADING_MODE_PASS_FAIL:
        return GRADING_MODE_PASS_FAIL
    if mode == GRADING_MODE_NUMERIC:
        return GRADING_MODE_NUMERIC
    raise GradeRecordUnavailableError


def allowed_marks_for_mode(grading_mode: str) -> list[str]:
    if grading_mode == GRADING_MODE_PASS_FAIL:
        return list(PASS_FAIL_ALLOWED_MARKS)
    return list(NUMERIC_ALLOWED_MARKS)


def _normalize_mark_for_mode(value: object, *, grading_mode: str) -> str:
    if isinstance(value, bool) or value is None:
        if grading_mode == GRADING_MODE_PASS_FAIL:
            raise ValueError("Mark must be PASS or FAIL.")
        raise ValueError(
            "Mark must be between 1.00 and 5.00 on the grading scale, or INC."
        )
    raw = str(value).strip()
    if not raw:
        raise ValueError("Mark must not be blank.")
    upper = raw.upper()
    if grading_mode == GRADING_MODE_PASS_FAIL:
        if upper in PASS_FAIL_MARKS:
            return upper
        if upper == "INC":
            raise _MarkModeMismatch("INC is not allowed on a PASS_FAIL section.")
        try:
            numeric = Decimal(raw)
        except InvalidOperation as exc:
            raise ValueError("Mark must be PASS or FAIL.") from exc
        if numeric.is_finite():
            raise _MarkModeMismatch(
                "Numeric marks are not allowed on a PASS_FAIL section."
            )
        raise ValueError("Mark must be PASS or FAIL.")
    if upper in PASS_FAIL_MARKS:
        raise _MarkModeMismatch(f"{upper} is not allowed on a NUMERIC section.")
    return _normalize_mark(value)


async def sections_have_marks(
    db: AsyncSession, section_ids: Collection[uuid.UUID]
) -> bool:
    """Return True when any enrollment in ``section_ids`` has saved or locked marks."""
    if not section_ids:
        return False
    enrollments = await enrollment.list_sections_enrollments(db, set(section_ids))
    if not enrollments:
        return False
    return await repository.any_marks_for_enrollments(
        db, {row.enrollment_id for row in enrollments}
    )


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
                completion_deadline=(
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
    grading_mode = _section_grading_mode(course)
    return SectionGradesRead(
        section_id=section.id,
        section_name=section.section_name,
        course_code=course.course_code if course else None,
        course_title=course.title if course else None,
        grading_mode=grading_mode,
        allowed_marks=allowed_marks_for_mode(grading_mode),
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

        grading_mode = _section_grading_mode(course)
        allowed_marks = allowed_marks_for_mode(grading_mode)
        row_errors: list[dict[str, object]] = []
        mismatch_errors: list[dict[str, object]] = []
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
                        updates["final_grade"] = _normalize_mark_for_mode(
                            item.final_grade, grading_mode=grading_mode
                        )
                    except _MarkModeMismatch as exc:
                        mismatch_errors.append(
                            {
                                **row_meta,
                                "field": "finalGrade",
                                "value": str(item.final_grade),
                                "message": str(exc),
                            }
                        )
                        continue
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

        if mismatch_errors:
            raise GradeModeMismatchError(
                grading_mode=grading_mode,
                allowed_marks=allowed_marks,
                row_errors=mismatch_errors,
            )
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
                if updates.get("final_grade") == "INC":
                    row.inc_expires_at = _inc_deadline_for(now)
                    row.completion_status = INC_STATE_PENDING
                grades_by_enrollment[target.enrollment_id] = row
            else:
                for key, val in updates.items():
                    setattr(row, key, val)
                row.is_draft = True
                row.is_locked = False
                if "final_grade" in updates:
                    if updates["final_grade"] == "INC":
                        row.inc_expires_at = _inc_deadline_for(now)
                        row.completion_status = INC_STATE_PENDING
                    else:
                        row.inc_expires_at = None
                        row.completion_status = None
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
    """Lock all enrolled grades atomically, recompute standing, audit, and notify."""
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

        grading_mode = _section_grading_mode(course)
        allowed_marks = allowed_marks_for_mode(grading_mode)
        grades_by_enrollment = {g.enrollment_item_id: g for g in existing_grades}
        row_errors: list[dict[str, object]] = []
        mismatch_errors: list[dict[str, object]] = []
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
                normalized_final_marks[membership.enrollment_id] = (
                    _normalize_mark_for_mode(
                        grade.final_grade, grading_mode=grading_mode
                    )
                )
            except _MarkModeMismatch as exc:
                mismatch_errors.append(
                    {
                        **row_meta,
                        "field": "finalGrade",
                        "value": str(grade.final_grade),
                        "message": str(exc),
                    }
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
        if mismatch_errors:
            raise GradeModeMismatchError(
                grading_mode=grading_mode,
                allowed_marks=allowed_marks,
                row_errors=mismatch_errors,
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

        inc_deadline = _inc_deadline_for(now)
        for membership in memberships:
            row = grades_by_enrollment[membership.enrollment_id]
            mark = normalized_final_marks[membership.enrollment_id]
            row.final_grade = mark
            row.inc_expires_at = inc_deadline if mark == "INC" else None
            row.completion_status = INC_STATE_PENDING if mark == "INC" else None
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

        for student_id in sorted(
            {membership.student_id for membership in memberships},
            key=str,
        ):
            await _recompute_student_term_evaluation(
                db,
                student_id=student_id,
                term_id=section.academic_term_id,
                evaluated_at=now,
            )

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


def _normalize_completion_mark(value: object) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError("Proposed mark must be between 1.00 and 3.00.")
    raw = str(value).strip()
    if not raw:
        raise ValueError("Proposed mark must not be blank.")
    try:
        numeric = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Proposed mark must be between 1.00 and 3.00.") from exc
    if not numeric.is_finite():
        raise ValueError("Proposed mark must be finite.")
    formatted = format(numeric, ".2f")
    if numeric != Decimal(formatted) or formatted not in NUMERIC_COMPLETION_MARKS:
        raise ValueError(
            "Proposed mark must be a passing grade from 1.00 to 3.00 in 0.25 steps."
        )
    return formatted


def _colleges_match(student_college: str | None, dean_college: str | None) -> bool:
    if not student_college or not dean_college:
        return False
    return student_college.strip().casefold() == dean_college.strip().casefold()


def _derive_inc_lifecycle_state(
    grade, now: datetime
) -> tuple[str, int | None, datetime | None]:
    deadline = utc(grade.inc_expires_at) if grade.inc_expires_at is not None else None
    mark = (grade.final_grade or "").strip().upper()
    if grade.completion_status == INC_STATE_COMPLETED:
        return INC_STATE_COMPLETED, None, deadline
    if grade.completion_status == INC_STATE_EXPIRED or mark == "5.00":
        return INC_STATE_EXPIRED, 0, deadline
    if deadline is not None and now >= deadline:
        return INC_STATE_EXPIRED, 0, deadline
    if deadline is not None:
        seconds_left = (deadline - now).total_seconds()
        days_left = max(0, math.ceil(seconds_left / 86400))
        return INC_STATE_PENDING, days_left, deadline
    return INC_STATE_PENDING, None, None


def _is_inc_window_closed(grade, now: datetime) -> bool:
    mark = (grade.final_grade or "").strip().upper()
    if grade.completion_status == INC_STATE_EXPIRED or mark == "5.00":
        return True
    return grade.inc_expires_at is not None and now >= utc(grade.inc_expires_at)


async def _load_inc_grade_context(
    db: AsyncSession, grades: Collection[object]
) -> tuple[
    dict[uuid.UUID, object],
    dict[uuid.UUID, object],
    dict[uuid.UUID, object],
    dict[uuid.UUID, object],
    dict[uuid.UUID, object],
    dict[uuid.UUID, object],
    dict[uuid.UUID, object],
]:
    enrollment_ids = {g.enrollment_item_id for g in grades}
    owners = await enrollment.get_enrollment_owners(db, enrollment_ids)
    section_ids = {o.section_id for o in owners.values()}
    term_ids = {o.academic_term_id for o in owners.values()}
    student_ids = {o.student_id for o in owners.values()}

    section_map = {s.id: s for s in await sections.record_sections(db, section_ids)}
    course_ids = {s.course_id for s in section_map.values()}
    course_map = {c.id: c for c in await curriculum.record_courses(db, course_ids)}
    term_map = {t.id: t for t in await academics.record_terms(db, term_ids)}
    student_map = await users.overload_student_identities(db, student_ids)
    program_ids = {s.program_id for s in student_map.values()}
    program_map = await curriculum.programs_by_ids(db, program_ids)
    completions = await repository.list_completions_for_grades(
        db, {g.id for g in grades}
    )
    latest_completion_by_grade: dict[uuid.UUID, object] = {}
    for comp in completions:
        latest_completion_by_grade.setdefault(comp.grade_id, comp)
    return (
        owners,
        section_map,
        course_map,
        term_map,
        student_map,
        program_map,
        latest_completion_by_grade,
    )


def _build_incomplete_grade_read(
    grade,
    *,
    owners,
    section_map,
    course_map,
    term_map,
    student_map,
    latest_completion_by_grade,
    now: datetime,
) -> IncompleteGradeRead | None:
    owner = owners.get(grade.enrollment_item_id)
    if owner is None:
        return None
    section = section_map.get(owner.section_id)
    course = course_map.get(section.course_id) if section is not None else None
    term = term_map.get(owner.academic_term_id)
    student = student_map.get(owner.student_id)
    if section is None or course is None or term is None or student is None:
        return None
    state, days_left, deadline = _derive_inc_lifecycle_state(grade, now)
    latest = latest_completion_by_grade.get(grade.id)
    raw_mark = (grade.final_grade or "").strip()
    display_mark = "Inc" if raw_mark.upper() == "INC" else raw_mark
    return IncompleteGradeRead(
        inc_id=grade.id,
        grade_id=grade.id,
        enrollment_id=grade.enrollment_item_id,
        section_id=section.id,
        section_name=section.section_name,
        course_id=course.id,
        course_code=course.course_code,
        course_title=course.title,
        academic_term_id=term.id,
        academic_year=term.academic_year,
        semester=term.semester,
        student_id=owner.student_id,
        student_number=student.student_number,
        student_name=student.full_name,
        final_grade=display_mark,
        submitted_at=utc(grade.submitted_at) if grade.submitted_at else None,
        completion_deadline=deadline,
        days_left=days_left,
        state=state,  # type: ignore[arg-type]
        latest_completion=(
            IncompleteCompletionSummaryRead.model_validate(latest)
            if latest is not None
            else None
        ),
    )


def _build_incomplete_completion_read(
    completion,
    *,
    grade,
    owners,
    section_map,
    course_map,
    term_map,
    student_map,
    program_map,
) -> IncompleteCompletionRead | None:
    if grade is None:
        return None
    owner = owners.get(grade.enrollment_item_id)
    if owner is None:
        return None
    section = section_map.get(owner.section_id)
    course = course_map.get(section.course_id) if section is not None else None
    term = term_map.get(owner.academic_term_id)
    student = student_map.get(owner.student_id)
    if section is None or course is None or term is None or student is None:
        return None
    program = program_map.get(student.program_id)
    deadline = utc(grade.inc_expires_at) if grade.inc_expires_at is not None else None
    return IncompleteCompletionRead(
        id=completion.id,
        completion_id=completion.id,
        inc_id=grade.id,
        grade_id=grade.id,
        enrollment_id=grade.enrollment_item_id,
        section_id=section.id,
        section_name=section.section_name,
        course_id=course.id,
        course_code=course.course_code,
        course_title=course.title,
        academic_term_id=term.id,
        academic_year=term.academic_year,
        semester=term.semester,
        student_id=owner.student_id,
        student_number=student.student_number,
        student_name=student.full_name,
        college=program.college if program is not None else None,
        proposed_mark=completion.proposed_mark,
        completion_form_file_id=completion.completion_form_file_id,
        remarks=completion.remarks,
        state=completion.state,  # type: ignore[arg-type]
        version=completion.version,
        reviewed_by=completion.reviewed_by,
        review_remarks=completion.review_remarks,
        resolved_at=utc(completion.resolved_at) if completion.resolved_at else None,
        completion_deadline=deadline,
        created_at=utc(completion.created_at),
    )


def _evaluate_term_standing(
    term_courses: list[GradeCourse], *, prior_standing: str | None = None
) -> str:
    total_units = sum((c.units for c in term_courses if c.units > 0), Decimal("0"))
    failed_units = sum(
        (
            c.units
            for c in term_courses
            if c.final_grade in {"5.00", "FAIL"} and c.units > 0
        ),
        Decimal("0"),
    )
    if total_units <= 0 or failed_units <= 0:
        return AcademicStanding.GOOD.value
    ratio = failed_units / total_units
    if ratio < Decimal("0.25"):
        return AcademicStanding.GOOD.value
    if ratio < Decimal("0.50"):
        if prior_standing == AcademicStanding.WARNING_1.value:
            return AcademicStanding.WARNING_2.value
        return AcademicStanding.WARNING_1.value
    if ratio <= Decimal("0.75"):
        return AcademicStanding.PROBATION.value
    if ratio < Decimal("1.00"):
        return AcademicStanding.COLLEGE_DROPPING.value
    return AcademicStanding.UNIVERSITY_DISMISSAL.value


async def _recompute_student_term_evaluation(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    term_id: uuid.UUID,
    evaluated_at: datetime,
) -> None:
    enrolled = await enrollment.record_enrollments(db, student_id)
    term_ids = {row.academic_term_id for row in enrolled}
    terms = await academics.record_terms(db, term_ids)
    terms.sort(key=lambda row: (row.start_date, row.end_date, str(row.id)))
    attempts = {row.id: row for row in enrolled}
    marks = await repository.published_marks(db, list(attempts))
    section_rows = {
        row.id: row
        for row in await sections.record_sections(
            db, {attempts[m.enrollment_item_id].section_id for m in marks}
        )
    }
    catalog = {
        row.id: row
        for row in await curriculum.record_courses(
            db, {row.course_id for row in section_rows.values()}
        )
    }
    by_term: dict[uuid.UUID, list[GradeCourse]] = {t.id: [] for t in terms}
    for mark in marks:
        attempt = attempts[mark.enrollment_item_id]
        section = section_rows.get(attempt.section_id)
        course = catalog.get(section.course_id) if section is not None else None
        if section is None or course is None:
            continue
        by_term.setdefault(attempt.academic_term_id, []).append(
            _course(mark, section, course)
        )

    existing_evals = {
        row.academic_term_id: row
        for row in await repository.list_student_term_gpas(db, student_id=student_id)
    }
    cum_num, cum_den = Decimal("0"), Decimal("0")
    prior_standing: str | None = None
    target_term_data: tuple[Decimal, Decimal, Decimal, str] | None = None

    for term in terms:
        courses = by_term.get(term.id, [])
        term_num, term_den = weighted_totals(courses)
        cum_num += term_num
        cum_den += term_den
        if term.id == term_id:
            term_gwa = average(term_num, term_den) or Decimal("0.00")
            cum_gwa = average(cum_num, cum_den) or Decimal("0.00")
            standing = _evaluate_term_standing(courses, prior_standing=prior_standing)
            target_term_data = (
                term_den.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP),
                term_gwa.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                cum_gwa.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                standing,
            )
            break
        if prev_eval := existing_evals.get(term.id):
            prior_standing = prev_eval.academic_standing

    if target_term_data is None:
        return
    total_gpa_units, term_gpa, cumulative_gpa, academic_standing = target_term_data
    await repository.upsert_student_term_gpa(
        db,
        student_id=student_id,
        academic_term_id=term_id,
        total_gpa_units=total_gpa_units,
        term_gpa=term_gpa,
        cumulative_gpa=cumulative_gpa,
        academic_standing=academic_standing,
        calculated_at=evaluated_at,
    )


async def list_section_incompletes(
    db: AsyncSession,
    *,
    faculty_id: uuid.UUID,
    section_id: uuid.UUID,
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Page[IncompleteGradeRead]:
    """List finalized INC rows for one section assigned to ``faculty_id``."""
    await sections.get_owned_section(db, faculty_id=faculty_id, section_id=section_id)
    memberships = [
        m
        for m in await enrollment.list_sections_enrollments(db, {section_id})
        if _is_enrolled(m.status)
    ]
    grades = await repository.list_incomplete_grades_for_enrollments(
        db, {m.enrollment_id for m in memberships}
    )
    if not grades:
        return Page[IncompleteGradeRead](items=[], total=0, limit=limit, offset=offset)

    now = datetime.now(UTC)
    (
        owners,
        section_map,
        course_map,
        term_map,
        student_map,
        _program_map,
        latest_completion_by_grade,
    ) = await _load_inc_grade_context(db, grades)
    items = [
        item
        for grade in grades
        if (
            item := _build_incomplete_grade_read(
                grade,
                owners=owners,
                section_map=section_map,
                course_map=course_map,
                term_map=term_map,
                student_map=student_map,
                latest_completion_by_grade=latest_completion_by_grade,
                now=now,
            )
        )
        is not None
        and (state is None or item.state == state)
    ]
    items.sort(
        key=lambda r: (r.student_name.casefold(), r.student_number, str(r.inc_id))
    )
    total = len(items)
    return Page[IncompleteGradeRead](
        items=items[offset : offset + limit],
        total=total,
        limit=limit,
        offset=offset,
    )


async def list_faculty_incompletes(
    db: AsyncSession,
    *,
    faculty_id: uuid.UUID,
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Page[IncompleteGradeRead]:
    """List finalized INC rows across all sections assigned to ``faculty_id``."""
    owned_sections = await sections.list_owned_sections(db, faculty_id=faculty_id)
    if not owned_sections:
        return Page[IncompleteGradeRead](items=[], total=0, limit=limit, offset=offset)
    memberships = [
        m
        for m in await enrollment.list_sections_enrollments(
            db, {s.id for s in owned_sections}
        )
        if _is_enrolled(m.status)
    ]
    grades = await repository.list_incomplete_grades_for_enrollments(
        db, {m.enrollment_id for m in memberships}
    )
    if not grades:
        return Page[IncompleteGradeRead](items=[], total=0, limit=limit, offset=offset)

    now = datetime.now(UTC)
    (
        owners,
        section_map,
        course_map,
        term_map,
        student_map,
        _program_map,
        latest_completion_by_grade,
    ) = await _load_inc_grade_context(db, grades)
    items = [
        item
        for grade in grades
        if (
            item := _build_incomplete_grade_read(
                grade,
                owners=owners,
                section_map=section_map,
                course_map=course_map,
                term_map=term_map,
                student_map=student_map,
                latest_completion_by_grade=latest_completion_by_grade,
                now=now,
            )
        )
        is not None
        and (state is None or item.state == state)
    ]
    items.sort(
        key=lambda r: (
            r.course_code,
            r.section_name,
            r.student_name.casefold(),
            str(r.inc_id),
        )
    )
    total = len(items)
    return Page[IncompleteGradeRead](
        items=items[offset : offset + limit],
        total=total,
        limit=limit,
        offset=offset,
    )


async def file_incomplete_completion(
    db: AsyncSession,
    *,
    faculty_id: uuid.UUID,
    inc_id: uuid.UUID,
    payload: CreateIncompleteCompletionRequest,
    client_ip: str | None = None,
) -> IncompleteCompletionRead:
    """File a passing completion grade (1.00-3.00) and form for an Inc row."""
    now = datetime.now(UTC)
    try:
        grade = await repository.get_student_grade_by_id(db, inc_id, lock=True)
        if grade is None or grade.is_draft:
            raise ResourceNotFoundError
        owners = await enrollment.get_enrollment_owners(db, {grade.enrollment_item_id})
        owner = owners.get(grade.enrollment_item_id)
        if owner is None:
            raise ResourceNotFoundError
        await sections.get_owned_section(
            db, faculty_id=faculty_id, section_id=owner.section_id
        )
        is_inc_row = (grade.final_grade or "").strip().upper() == "INC" or (
            grade.completion_status
            in {INC_STATE_PENDING, INC_STATE_COMPLETED, INC_STATE_EXPIRED}
        )
        if not is_inc_row:
            raise ResourceNotFoundError

        if _is_inc_window_closed(grade, now):
            raise IncCompletionWindowClosedError
        if grade.completion_status == INC_STATE_COMPLETED:
            raise IncAlreadyCompletedError
        if await repository.get_pending_completion_for_grade(db, grade.id) is not None:
            raise IncCompletionPendingError

        form_id = (payload.completion_form_file_id or "").strip()
        if not form_id:
            raise IncompleteValidationError(
                "Completion form file ID is required.",
                field="completionFormFileId",
            )
        if len(form_id) > 100:
            raise IncompleteValidationError(
                "Completion form file ID must be at most 100 characters.",
                field="completionFormFileId",
            )
        try:
            proposed_mark = _normalize_completion_mark(payload.proposed_mark)
        except ValueError as exc:
            raise IncompleteValidationError(str(exc), field="proposedMark") from exc

        remarks = payload.remarks.strip() if payload.remarks is not None else None
        completion = repository.add_incomplete_completion(
            db,
            grade_id=grade.id,
            proposed_mark=proposed_mark,
            completion_form_file_id=form_id,
            remarks=remarks or None,
            created_at=now,
        )
        try:
            await db.flush()
        except IntegrityError as exc:
            raise IncCompletionPendingError from exc

        audit.stage(
            db,
            action=INC_COMPLETION_FILED_ACTION,
            actor_id=faculty_id,
            target_type="incomplete_completion",
            target_id=completion.id,
            occurred_at=now,
            prior_state={
                "finalGrade": grade.final_grade,
                "state": grade.completion_status,
            },
            new_state={
                "proposedMark": proposed_mark,
                "completionFormFileId": form_id,
                "state": COMPLETION_STATE_PENDING_DEAN,
            },
            payload={
                "incId": str(grade.id),
                "enrollmentId": str(grade.enrollment_item_id),
                "sectionId": str(owner.section_id),
                "studentId": str(owner.student_id),
                "actorId": str(faculty_id),
                "role": "faculty",
                "ip_address": client_ip,
                "timestampUtc8": _utc8_iso(now),
            },
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    (
        owners_ctx,
        section_map,
        course_map,
        term_map,
        student_map,
        program_map,
        _latest,
    ) = await _load_inc_grade_context(db, [grade])
    result = _build_incomplete_completion_read(
        completion,
        grade=grade,
        owners=owners_ctx,
        section_map=section_map,
        course_map=course_map,
        term_map=term_map,
        student_map=student_map,
        program_map=program_map,
    )
    if result is None:  # pragma: no cover - hydrated from already-verified owners
        raise GradeRecordUnavailableError
    return result


async def list_admin_incomplete_completions(
    db: AsyncSession,
    *,
    position: str,
    college_id: str | None,
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Page[IncompleteCompletionRead]:
    """List college-scoped incomplete completion requests for a College Dean."""
    if position != "dean":
        raise ForbiddenError(
            "Only a College Dean may review incomplete completion requests."
        )
    completions = await repository.list_incomplete_completions(db, state=state)
    if not completions or not college_id:
        return Page[IncompleteCompletionRead](
            items=[], total=0, limit=limit, offset=offset
        )

    grades = await repository.list_grades_by_ids(db, {c.grade_id for c in completions})
    grade_by_id = {g.id: g for g in grades}
    (
        owners,
        section_map,
        course_map,
        term_map,
        student_map,
        program_map,
        _latest,
    ) = await _load_inc_grade_context(db, grades)

    items = [
        dto
        for comp in completions
        if (
            dto := _build_incomplete_completion_read(
                comp,
                grade=grade_by_id.get(comp.grade_id),
                owners=owners,
                section_map=section_map,
                course_map=course_map,
                term_map=term_map,
                student_map=student_map,
                program_map=program_map,
            )
        )
        is not None
        and _colleges_match(dto.college, college_id)
    ]
    total = len(items)
    return Page[IncompleteCompletionRead](
        items=items[offset : offset + limit],
        total=total,
        limit=limit,
        offset=offset,
    )


async def decide_incomplete_completion(
    db: AsyncSession,
    *,
    completion_id: uuid.UUID,
    reviewer_id: uuid.UUID,
    position: str,
    college_id: str | None,
    payload: IncompleteCompletionDecisionRequest,
    client_ip: str | None = None,
) -> IncompleteCompletionRead:
    """Endorse or reject an Incomplete completion within the Dean's own college."""
    if position != "dean":
        raise ForbiddenError(
            "Only a College Dean may endorse or reject incomplete completions."
        )
    now = datetime.now(UTC)
    try:
        completion = await repository.get_incomplete_completion(
            db, completion_id, lock=True
        )
        if completion is None:
            raise ResourceNotFoundError
        grade = await repository.get_student_grade_by_id(
            db, completion.grade_id, lock=True
        )
        if grade is None:
            raise ResourceNotFoundError

        (
            owners,
            section_map,
            course_map,
            term_map,
            student_map,
            program_map,
            _latest,
        ) = await _load_inc_grade_context(db, [grade])
        dto = _build_incomplete_completion_read(
            completion,
            grade=grade,
            owners=owners,
            section_map=section_map,
            course_map=course_map,
            term_map=term_map,
            student_map=student_map,
            program_map=program_map,
        )
        if dto is None or not _colleges_match(dto.college, college_id):
            raise ResourceNotFoundError

        if payload.version is not None and completion.version != payload.version:
            raise GradeVersionConflictError(current_version=completion.version)
        if completion.state == COMPLETION_STATE_EXPIRED or _is_inc_window_closed(
            grade, now
        ):
            raise IncCompletionWindowClosedError
        if completion.state != COMPLETION_STATE_PENDING_DEAN:
            raise IncCompletionAlreadyDecidedError

        normalized_remarks = (
            payload.remarks.strip() if payload.remarks is not None else None
        )
        if payload.decision == "REJECT" and not normalized_remarks:
            raise IncompleteDecisionRemarksRequiredError

        owner = owners[grade.enrollment_item_id]
        section = section_map[owner.section_id]
        course = course_map[section.course_id]

        if payload.decision == "ENDORSE":
            prior_state = {
                "completionState": completion.state,
                "finalGrade": grade.final_grade,
                "completionStatus": grade.completion_status,
                "incExpiresAt": (
                    utc(grade.inc_expires_at).isoformat()
                    if grade.inc_expires_at is not None
                    else None
                ),
            }
            grade.final_grade = completion.proposed_mark
            grade.inc_expires_at = None
            grade.completion_status = INC_STATE_COMPLETED
            grade.updated_at = now
            completion.state = COMPLETION_STATE_ENDORSED
            completion.version += 1
            completion.reviewed_by = reviewer_id
            completion.review_remarks = normalized_remarks or None
            completion.resolved_at = now
            await db.flush()
            await _recompute_student_term_evaluation(
                db,
                student_id=owner.student_id,
                term_id=owner.academic_term_id,
                evaluated_at=now,
            )
            audit.stage(
                db,
                action=INC_COMPLETION_ENDORSED_ACTION,
                actor_id=reviewer_id,
                target_type="incomplete_completion",
                target_id=completion.id,
                occurred_at=now,
                prior_state=prior_state,
                new_state={
                    "completionState": completion.state,
                    "finalGrade": grade.final_grade,
                    "completionStatus": grade.completion_status,
                    "incExpiresAt": None,
                },
                payload={
                    "incId": str(grade.id),
                    "studentId": str(owner.student_id),
                    "sectionId": str(section.id),
                    "college": dto.college,
                    "actorId": str(reviewer_id),
                    "role": position,
                    "ip_address": client_ip,
                    "timestampUtc8": _utc8_iso(now),
                },
            )
            recipients = [owner.student_id]
            if section.faculty_id and section.faculty_id != owner.student_id:
                recipients.append(section.faculty_id)
            for recipient_id in recipients:
                publish_transactional(
                    db,
                    Event(
                        name=INC_COMPLETION_DECIDED_EVENT,
                        payload={
                            "user_id": str(recipient_id),
                            "completion_id": str(completion.id),
                            "inc_id": str(grade.id),
                            "state": COMPLETION_STATE_ENDORSED,
                            "course_code": course.course_code,
                            "proposed_mark": completion.proposed_mark,
                            "remarks": normalized_remarks,
                        },
                    ),
                )
        else:
            prior_state = {"completionState": completion.state}
            completion.state = COMPLETION_STATE_REJECTED
            completion.version += 1
            completion.reviewed_by = reviewer_id
            completion.review_remarks = normalized_remarks
            completion.resolved_at = now
            await db.flush()
            audit.stage(
                db,
                action=INC_COMPLETION_REJECTED_ACTION,
                actor_id=reviewer_id,
                target_type="incomplete_completion",
                target_id=completion.id,
                occurred_at=now,
                prior_state=prior_state,
                new_state={
                    "completionState": completion.state,
                    "reviewRemarks": normalized_remarks,
                },
                payload={
                    "incId": str(grade.id),
                    "studentId": str(owner.student_id),
                    "sectionId": str(section.id),
                    "college": dto.college,
                    "actorId": str(reviewer_id),
                    "role": position,
                    "ip_address": client_ip,
                    "timestampUtc8": _utc8_iso(now),
                },
            )
            if section.faculty_id:
                publish_transactional(
                    db,
                    Event(
                        name=INC_COMPLETION_DECIDED_EVENT,
                        payload={
                            "user_id": str(section.faculty_id),
                            "completion_id": str(completion.id),
                            "inc_id": str(grade.id),
                            "state": COMPLETION_STATE_REJECTED,
                            "course_code": course.course_code,
                            "proposed_mark": completion.proposed_mark,
                            "remarks": normalized_remarks,
                        },
                    ),
                )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    updated = _build_incomplete_completion_read(
        completion,
        grade=grade,
        owners=owners,
        section_map=section_map,
        course_map=course_map,
        term_map=term_map,
        student_map=student_map,
        program_map=program_map,
    )
    if updated is None:  # pragma: no cover - hydrated above
        raise GradeRecordUnavailableError
    return updated


async def expire_overdue_incompletes(
    db: AsyncSession,
    *,
    run_key: str | None = None,
    as_of: datetime | None = None,
) -> IncExpiryRunResult:
    """Convert overdue finalized INC marks to 5.00 once per run key."""
    now = utc(as_of) if as_of is not None else datetime.now(UTC)
    resolved_run_key = (
        run_key
        or f"inc-expiry-{now.astimezone(ZoneInfo('Asia/Manila')).date().isoformat()}"
    )
    existing = await repository.get_inc_expiry_run(db, resolved_run_key)
    if existing is not None and existing.status == "COMPLETED":
        return IncExpiryRunResult(
            run_key=resolved_run_key,
            processed=0,
            converted=0,
            skipped_duplicate_run=True,
        )

    run = existing or repository.stage_inc_expiry_run(
        db, resolved_run_key, started_at=now
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        run = await repository.get_inc_expiry_run(db, resolved_run_key)
        if run is None:
            raise
        if run.status == "COMPLETED":
            return IncExpiryRunResult(
                run_key=resolved_run_key,
                processed=0,
                converted=0,
                skipped_duplicate_run=True,
            )

    converted = 0
    try:
        overdue = await repository.list_overdue_inc_grades_for_update(db, cutoff=now)
        if overdue:
            owners = await enrollment.get_enrollment_owners(
                db, {g.enrollment_item_id for g in overdue}
            )
            section_map = {
                s.id: s
                for s in await sections.record_sections(
                    db, {o.section_id for o in owners.values()}
                )
            }
            course_map = {
                c.id: c
                for c in await curriculum.record_courses(
                    db, {s.course_id for s in section_map.values()}
                )
            }
            affected_terms: set[tuple[uuid.UUID, uuid.UUID]] = set()

            for grade in overdue:
                prior_deadline = (
                    utc(grade.inc_expires_at).isoformat()
                    if grade.inc_expires_at is not None
                    else None
                )
                prior_state = {
                    "finalGrade": grade.final_grade,
                    "completionStatus": grade.completion_status,
                    "incExpiresAt": prior_deadline,
                }
                grade.final_grade = "5.00"
                grade.completion_status = INC_STATE_EXPIRED
                grade.updated_at = now
                converted += 1

                for pending in await repository.list_pending_completions_for_grade(
                    db, grade.id, lock=True
                ):
                    pending.state = COMPLETION_STATE_EXPIRED
                    pending.resolved_at = now

                owner = owners.get(grade.enrollment_item_id)
                if owner is not None:
                    affected_terms.add((owner.student_id, owner.academic_term_id))
                    section = section_map.get(owner.section_id)
                    course = (
                        course_map.get(section.course_id)
                        if section is not None
                        else None
                    )
                    publish_transactional(
                        db,
                        Event(
                            name=INC_EXPIRED_EVENT,
                            payload={
                                "user_id": str(owner.student_id),
                                "inc_id": str(grade.id),
                                "enrollment_id": str(grade.enrollment_item_id),
                                "course_code": course.course_code if course else None,
                            },
                        ),
                    )

                audit.stage(
                    db,
                    action=INC_EXPIRED_ACTION,
                    actor_id=None,
                    target_type="student_grade",
                    target_id=grade.id,
                    occurred_at=now,
                    prior_state=prior_state,
                    new_state={
                        "finalGrade": "5.00",
                        "completionStatus": INC_STATE_EXPIRED,
                    },
                    payload={
                        "actor": "system",
                        "runKey": resolved_run_key,
                        "enrollmentId": str(grade.enrollment_item_id),
                        "studentId": str(owner.student_id) if owner else None,
                        "academicTermId": (
                            str(owner.academic_term_id) if owner else None
                        ),
                        "timestampUtc8": _utc8_iso(now),
                    },
                )

            await db.flush()
            for student_id, term_id in sorted(
                affected_terms, key=lambda pair: (str(pair[0]), str(pair[1]))
            ):
                await _recompute_student_term_evaluation(
                    db,
                    student_id=student_id,
                    term_id=term_id,
                    evaluated_at=now,
                )

        run_row = await repository.get_inc_expiry_run(db, resolved_run_key)
        if run_row is None:  # pragma: no cover - created above
            raise RuntimeError("INC expiry run marker is missing")
        run_row.status = "COMPLETED"
        run_row.processed_count = len(overdue)
        run_row.converted_count = converted
        run_row.completed_at = now
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return IncExpiryRunResult(
        run_key=resolved_run_key,
        processed=len(overdue),
        converted=converted,
        skipped_duplicate_run=False,
    )
