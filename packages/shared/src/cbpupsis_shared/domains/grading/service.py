"""Grade, standing, and transcript reads, without workflow mutations."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.exceptions import ValidationError
from cbpupsis_shared.domains.academics import client as academics
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.curriculum import client as curriculum
from cbpupsis_shared.domains.enrollment import client as enrollment
from cbpupsis_shared.domains.grading import repository
from cbpupsis_shared.domains.grading.constants import (
    NUMERIC_MARKS,
    UNIT_REDUCTIONS,
    AcademicStanding,
)
from cbpupsis_shared.domains.grading.exceptions import GradeRecordUnavailableError
from cbpupsis_shared.domains.grading.schemas import (
    GradeCourse,
    GradesRead,
    ScholasticStandingRead,
    TranscriptRead,
    TranscriptTerm,
)
from cbpupsis_shared.domains.sections import client as sections
from cbpupsis_shared.domains.users import client as users

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
):
    identity = await users.record_identity(db, student_id)
    if identity is None or (
        student_number is not None and student_number != identity.student_number
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
