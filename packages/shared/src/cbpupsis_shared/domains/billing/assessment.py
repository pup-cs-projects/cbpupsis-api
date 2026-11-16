"""Reproducible provisional assessment policy shared by enrollment and finance."""

import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, DecimalException

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.audit import client as audit
from cbpupsis_shared.domains.billing import repository
from cbpupsis_shared.domains.billing.schemas import (
    AssessmentCourse,
    AssessmentInputs,
    AssessmentLine,
    AssessmentRead,
    StoredAssessment,
)
from cbpupsis_shared.domains.users import client as users
from cbpupsis_shared.domains.users.schemas import StudentProfileRead


class AssessmentInvalid(ValueError):
    """No complete, reproducible assessment is available. Contains no records."""


def covered_fee_codes(inputs: AssessmentInputs) -> set[str]:
    """Use configured coverage or the PM-approved four-fee default, never fines."""
    if not inputs.fhe_qualified or inputs.fhe_forfeited:
        return set()
    if not inputs.subsidy_covered_fees:
        return {"TUITION", "REGISTRATION", "LAB", "MISC"}
    codes = {code.strip().upper() for code in inputs.subsidy_covered_fees}
    aliases = {
        "LABORATORY": "LAB",
        "LAB_FEE": "LAB",
        "TUITION_FEE": "TUITION",
        "REGISTRATION_FEE": "REGISTRATION",
        "MISC_FEE": "MISC",
        "MISCELLANEOUS": "MISC",
    }
    return codes | {aliases[code] for code in codes if code in aliases}


def calculate_assessment(inputs: AssessmentInputs) -> AssessmentRead:
    """Round each configured line half-up, then sum. No balances or penalties."""
    if len({c.section_id for c in inputs.courses}) != len(inputs.courses):
        raise AssessmentInvalid
    units = sum((c.units for c in inputs.courses), Decimal(0))
    amounts = (
        ("TUITION", inputs.tuition_per_unit * units),
        ("REGISTRATION", inputs.registration_fee),
        ("LAB", inputs.lab_fee),
        ("MISC", inputs.misc_fee),
    )
    lines = [
        AssessmentLine(
            code=code,
            amount=format(
                value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f"
            ),
        )
        for code, value in amounts
    ]
    total = sum((Decimal(line.amount) for line in lines), Decimal(0))
    if total > Decimal("99999999.99"):
        raise AssessmentInvalid
    covered = covered_fee_codes(inputs)
    subsidy = sum(
        (Decimal(line.amount) for line in lines if line.code in covered), Decimal(0)
    )
    return AssessmentRead(
        lines=lines,
        amountAssessed=f"{total:.2f}",
        subsidyApplied=f"{subsidy:.2f}",
        netPayable=f"{total - subsidy:.2f}",
        fheQualified=inputs.fhe_qualified,
        fheForfeited=inputs.fhe_forfeited,
        fheForfeitureReason=inputs.fhe_forfeiture_reason,
    )


async def stage_assessment(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    term_id: uuid.UUID,
    courses: list[AssessmentCourse],
    due_date: date,
) -> StoredAssessment:
    """Caller owns the enrollment lock/transaction; this never commits.

    #169 calls after seat allocation; #101 after approved changes, before reissue.
    Qualification is stored users-domain policy, not a decision made here.
    """
    previous = await repository.get_student_billing_account(
        db, student_id, term_id, for_update=True
    )
    profile = await users.get_student_profile(db, student_id)
    if profile is None:
        raise AssessmentInvalid
    fee = await repository.get_fee_structure(db, profile.program_id, term_id)
    if fee is None:
        raise AssessmentInvalid
    inputs = AssessmentInputs(
        student_id=student_id,
        term_id=term_id,
        program_id=profile.program_id,
        fee_structure_id=fee.id,
        courses=courses,
        tuition_per_unit=fee.tuition_per_unit,
        registration_fee=fee.registration_fee,
        lab_fee=fee.lab_fee,
        misc_fee=fee.misc_fee,
        fhe_qualified=profile.fhe_qualified,
        fhe_forfeited=profile.fhe_forfeited,
        fhe_forfeiture_reason=profile.fhe_forfeiture_reason,
        subsidy_covered_fees=fee.subsidy_covered_fees,
    )
    stored = StoredAssessment(inputs=inputs, assessment=calculate_assessment(inputs))
    prior_subsidy = previous.fhe_subsidy_amount if previous else Decimal(0)
    row = await repository.store_assessment(db, stored, fee.due_date or due_date)
    stage_subsidy_audit(db, stored, prior_subsidy, account_id=row.id)
    return stored


def stage_subsidy_audit(
    db, stored: StoredAssessment, prior_subsidy: Decimal, *, account_id: uuid.UUID
) -> None:
    """Stage only actual monetary transitions; the caller owns the transaction."""
    subsidy = Decimal(stored.assessment.subsidyApplied)
    if subsidy == prior_subsidy:
        return
    now = datetime.now(UTC)
    audit.stage(
        db,
        action="billing.subsidy_applied"
        if subsidy > prior_subsidy
        else "billing.subsidy_withdrawn",
        # This is an automatic calculation, not a student-authored policy change.
        actor_id=None,
        target_type="student_billing_account",
        target_id=account_id,
        prior_state={"subsidyApplied": f"{prior_subsidy:.2f}"},
        new_state={
            "subsidyApplied": f"{subsidy:.2f}",
            "fheQualified": stored.inputs.fhe_qualified,
            "fheForfeited": stored.inputs.fhe_forfeited,
            "fheForfeitureReason": stored.inputs.fhe_forfeiture_reason,
        },
        payload={
            "rule": "STORED_FHE_DECISION_AND_CONFIGURED_COVERED_FEES",
            "studentId": str(stored.inputs.student_id),
            "coveredFeeCodes": sorted(covered_fee_codes(stored.inputs)),
            "feeStructureId": str(stored.inputs.fee_structure_id),
            "termId": str(stored.inputs.term_id),
            "timeUtc8": now.astimezone(timezone(timedelta(hours=8))).isoformat(),
        },
        occurred_at=now,
    )


async def refresh_current_subsidy(
    db: AsyncSession, profile: StudentProfileRead, term_id: uuid.UUID
) -> StoredAssessment | None:
    """Reconcile approved current-term decisions without repricing historical fees.

    The account lock serializes transitions with producer updates. Caller owns
    commit/rollback, and existing certificate snapshots are never touched.
    """
    row = await repository.get_student_billing_account(
        db, profile.user_id, term_id, for_update=True
    )
    if row is None:
        return None
    # A request may have waited behind a newer computation. The users client
    # refreshes its ORM identity; do not apply a decision captured before the lock.
    profile = await users.get_student_profile(db, profile.user_id)
    if profile is None:
        return None
    stored = await read_stored_assessment(db, profile.user_id, term_id)
    if stored is None or stored.inputs.program_id != profile.program_id:
        return None
    inputs = stored.inputs.model_copy(
        update={
            "fhe_qualified": profile.fhe_qualified,
            "fhe_forfeited": profile.fhe_forfeited,
            "fhe_forfeiture_reason": profile.fhe_forfeiture_reason,
        }
    )
    if inputs != stored.inputs:
        prior_subsidy = row.fhe_subsidy_amount
        stored = StoredAssessment(
            inputs=inputs, assessment=calculate_assessment(inputs)
        )
        await repository.store_assessment(db, stored, row.due_date)
        stage_subsidy_audit(db, stored, prior_subsidy, account_id=row.id)
    return stored


async def read_stored_assessment(db, student_id, term_id) -> StoredAssessment | None:
    account = await repository.get_student_billing_account(db, student_id, term_id)
    if account is None or account.assessment_snapshot is None:
        return None
    try:
        stored = StoredAssessment.model_validate(account.assessment_snapshot)
        if (
            stored.version != 1
            or stored.inputs.student_id != student_id
            or stored.inputs.term_id != term_id
            or calculate_assessment(stored.inputs) != stored.assessment
            or Decimal(stored.assessment.amountAssessed) != account.gross_amount
            or Decimal(stored.assessment.subsidyApplied) != account.fhe_subsidy_amount
        ):
            return None
        return stored
    except (ValueError, ValidationError, DecimalException):
        return None
