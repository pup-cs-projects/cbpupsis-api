from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.academics import client as academics
from cbpupsis_shared.domains.academics.schemas import AcademicTermRead
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.billing import client as billing
from cbpupsis_shared.domains.curriculum import client as curriculum
from cbpupsis_shared.domains.enrollment import client as enrollment
from cbpupsis_shared.domains.enrollment.constants import (
    BLOCKED_ACADEMIC_STANDINGS,
    SUMMER_RESTRICTED_ADMISSION_STATUSES,
    SUMMER_SUBJECT_CAP,
)
from cbpupsis_shared.domains.enrollment.schemas import (
    CeilingStudent,
    EligibilityHoldRead,
    EnrollmentEligibilityRead,
    EnrollmentEligibilityReason,
    EnrollmentPeriodRead,
)
from cbpupsis_shared.domains.grading import client as grading
from cbpupsis_shared.domains.users import client as users


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _term_type(term: AcademicTermRead) -> str:
    return "SUMMER" if "summer" in term.semester.lower() else "REGULAR"


def _period(term: AcademicTermRead, *, as_of: datetime) -> EnrollmentPeriodRead:
    start = term.early_enrollment_start or term.regular_enrollment_start
    end = term.late_enrollment_end or term.regular_enrollment_end
    if start is None or end is None:
        raise ResourceNotFoundError
    start_utc = _utc(start)
    end_utc = _utc(end)
    return EnrollmentPeriodRead(
        start=start_utc,
        end=end_utc,
        is_open=start_utc <= as_of <= end_utc,
    )


async def get_eligibility(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    as_of: datetime | None = None,
) -> EnrollmentEligibilityRead:
    profile = await users.get_student_profile(db, student_id)
    term = await academics.get_current_term(db)
    if profile is None or term is None:
        raise ResourceNotFoundError

    prescribed_load = await curriculum.get_prescribed_load(
        db,
        curriculum_id=profile.curriculum_id,
        year_level=profile.year_level,
        semester=term.semester,
    )
    approved_overload = await enrollment.get_approved_overload_units(
        db, student_id=student_id, academic_term_id=term.id
    )
    standing = await grading.get_student_scholastic_standing(db, student_id)
    transcript = await grading.read_transcript(
        db, student_id=student_id, complete=True, limit=100
    )
    program = await curriculum.get_program(db, profile.program_id)
    hold_rows = await billing.get_student_holds(
        db,
        student_id=student_id,
        has_financial_hold=profile.has_financial_hold,
        has_academic_hold=profile.has_academic_hold,
    )

    current_period = _period(term, as_of=_utc(as_of or datetime.now(UTC)))
    term_type = _term_type(term)
    ceiling_student = CeilingStudent(
        prescribed_load=prescribed_load,
        approved_overload_units=approved_overload,
        standing_reduction=Decimal(standing.unit_reduction),
        admission_status=profile.admission_status,
    )
    effective_ceiling = enrollment.compute_effective_ceiling(ceiling_student)

    holds = [
        EligibilityHoldRead(
            office=hold.office,
            type=hold.type,
            amount=Decimal(hold.amount_outstanding),
            is_blocking=hold.is_blocking,
        )
        for hold in hold_rows
    ]
    reasons: list[EnrollmentEligibilityReason] = []
    for hold in holds:
        if hold.is_blocking:
            reasons.append(
                EnrollmentEligibilityReason(
                    code=(
                        "ENROLL_FINANCIAL_HOLD"
                        if hold.type.upper() == "FINANCIAL"
                        else "ENROLL_HOLD_ACTIVE"
                    ),
                    message=(
                        f"Resolve the hold with the {hold.office} before enrolling."
                    ),
                )
            )
    if not current_period.is_open:
        reasons.append(
            EnrollmentEligibilityReason(
                code="ENROLL_PERIOD_CLOSED",
                message="Enrollment is outside the configured academic period.",
            )
        )
    if standing.value.upper() in BLOCKED_ACADEMIC_STANDINGS:
        reasons.append(
            EnrollmentEligibilityReason(
                code="ENROLL_STANDING_BLOCKED",
                message=f"Academic standing {standing.value} blocks enrollment.",
            )
        )
    if prescribed_load <= 0:
        reasons.append(
            EnrollmentEligibilityReason(
                code="ENROLL_CURRICULUM_LOAD_UNAVAILABLE",
                message="No prescribed curriculum load is configured for this term.",
            )
        )

    completion_ratio = (
        transcript.total_units_earned / program.total_units_required
        if program is not None and program.total_units_required > 0
        else Decimal("0")
    )
    last_regular_gwa = next(
        (
            block.gwa
            for block in transcript.terms
            if "summer" not in block.term.semester.lower() and block.gwa is not None
        ),
        None,
    )
    overload_eligible = completion_ratio >= Decimal("0.85") or bool(
        last_regular_gwa is not None and last_regular_gwa <= Decimal("1.75")
    )
    restricted_summer = (
        term_type == "SUMMER"
        and profile.admission_status.strip().upper()
        in SUMMER_RESTRICTED_ADMISSION_STATUSES
    )
    return EnrollmentEligibilityRead(
        is_eligible=not reasons,
        term_type=term_type,
        prescribed_load=prescribed_load,
        approved_overload_units=approved_overload,
        standing_reduction=ceiling_student.standing_reduction,
        effective_ceiling=effective_ceiling,
        academic_standing=standing.value,
        overload_eligible=overload_eligible,
        summer_subject_cap=SUMMER_SUBJECT_CAP if restricted_summer else None,
        holds=holds,
        academic_period=current_period,
        reasons=reasons,
    )
