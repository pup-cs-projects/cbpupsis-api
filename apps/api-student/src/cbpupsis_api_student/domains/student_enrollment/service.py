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
    AddDropRequestCreate,
    AddDropRequestRead,
    AvailableCourseRead,
    CeilingStudent,
    EligibilityHoldRead,
    EnrollmentEligibilityRead,
    EnrollmentEligibilityReason,
    EnrollmentPeriodRead,
    EnrollmentSubmitRead,
    OverloadRequestFiledRead,
)
from cbpupsis_shared.domains.grading import client as grading
from cbpupsis_shared.domains.users import client as users


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _term_type(term: AcademicTermRead) -> str:
    return "SUMMER" if "summer" in term.semester.lower() else "REGULAR"


def _add_drop_period(
    term: AcademicTermRead, *, as_of: datetime
) -> EnrollmentPeriodRead | None:
    # Same window the add and drop filing check enforces.
    start = term.regular_enrollment_start or term.early_enrollment_start
    end = term.add_drop_deadline
    if start is None or end is None:
        return None
    start_utc = _utc(start)
    end_utc = _utc(end)
    return EnrollmentPeriodRead(
        start=start_utc, end=end_utc, is_open=start_utc <= as_of <= end_utc
    )


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


async def _term(db: AsyncSession, academic_term_id: uuid.UUID) -> AcademicTermRead:
    term = await academics.get_term_by_id(db, academic_term_id)
    if term is None:
        raise ResourceNotFoundError
    return term


async def get_available_courses(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    academic_term_id: uuid.UUID,
) -> list[AvailableCourseRead]:
    term = await _term(db, academic_term_id)
    return await enrollment.list_available_courses(db, student_id=student_id, term=term)


async def submit_enrollment(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    academic_term_id: uuid.UUID,
    section_ids: list[uuid.UUID],
    client_ip: str | None,
    role: str,
) -> EnrollmentSubmitRead:
    term = await _term(db, academic_term_id)
    return await enrollment.submit_enrollment(
        db,
        student_id=student_id,
        term=term,
        section_ids=section_ids,
        client_ip=client_ip,
        role=role,
    )


async def file_overload_request(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    academic_term_id: uuid.UUID,
    requested_units: int,
    justification: str,
    client_ip: str | None,
    role: str,
) -> OverloadRequestFiledRead:
    term = await _term(db, academic_term_id)
    return await enrollment.file_overload_request(
        db,
        student_id=student_id,
        term=term,
        requested_units=requested_units,
        justification=justification,
        client_ip=client_ip,
        role=role,
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

    curr_read = (
        await curriculum.get_curriculum(db, profile.curriculum_id)
        if profile.curriculum_id is not None
        else None
    )
    prescribed_load = await curriculum.get_prescribed_load(
        db,
        curriculum_id=profile.curriculum_id,
        year_level=profile.year_level,
        semester=term.semester,
    )
    approved_overload = await enrollment.get_approved_overload_units(
        db, student_id=student_id, academic_term_id=term.id
    )
    pending_overload = await enrollment.get_pending_overload_units(
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

    now = _utc(as_of or datetime.now(UTC))
    current_period = _period(term, as_of=now)
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
                        (
                            f"Resolve the PHP {hold.amount} financial hold with "
                            f"the {hold.office} before enrolling."
                        )
                        if hold.type.upper() == "FINANCIAL"
                        else (
                            f"Resolve the {hold.type.lower()} hold with the "
                            f"{hold.office} before enrolling."
                        )
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

    last_regular_gwa = await grading.get_previous_regular_term_gwa(
        db, student_id=student_id, before=term.start_date
    )
    overload_assessment = enrollment.assess_overload_eligibility(
        total_units_earned=transcript.total_units_earned,
        total_units_required=(
            program.total_units_required if program is not None else Decimal("0")
        ),
        gwa=last_regular_gwa,
        stored_graduating=profile.is_graduating_senior,
    )
    restricted_summer = (
        term_type == "SUMMER"
        and profile.admission_status.strip().upper()
        in SUMMER_RESTRICTED_ADMISSION_STATUSES
    )
    return EnrollmentEligibilityRead(
        is_eligible=not reasons,
        term=f"{term.academic_year}-{term.semester}",
        academic_term_id=term.id,
        term_type=term_type,
        curriculum_id=profile.curriculum_id,
        curriculum_version=(
            curr_read.curriculum_year if curr_read is not None else None
        ),
        prescribed_load=prescribed_load,
        approved_overload_units=approved_overload,
        pending_overload_units=pending_overload,
        standing_reduction=ceiling_student.standing_reduction,
        effective_ceiling=effective_ceiling,
        academic_standing=standing.value,
        overload_eligible=overload_assessment.eligible,
        summer_subject_cap=SUMMER_SUBJECT_CAP if restricted_summer else None,
        holds=holds,
        academic_period=current_period,
        add_drop_period=_add_drop_period(term, as_of=now),
        reasons=reasons,
    )


async def file_add_drop_request(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    request_data: AddDropRequestCreate,
    client_ip: str | None,
) -> AddDropRequestRead:
    return await enrollment.file_add_drop_request(
        db,
        student_id=student_id,
        request_data=request_data,
        client_ip=client_ip,
    )


async def get_student_add_drop_request(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    request_id: uuid.UUID,
) -> AddDropRequestRead:
    return await enrollment.get_student_add_drop_request(
        db,
        student_id=student_id,
        request_id=request_id,
    )


async def list_student_add_drop_requests(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
) -> list[AddDropRequestRead]:
    return await enrollment.list_student_add_drop_requests(
        db,
        student_id=student_id,
    )
