"""Reproducible provisional assessment policy shared by enrollment and finance."""

import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, DecimalException

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.billing import repository
from cbpupsis_shared.domains.billing.schemas import (
    AssessmentCourse,
    AssessmentInputs,
    AssessmentLine,
    AssessmentRead,
    StoredAssessment,
)
from cbpupsis_shared.domains.users import client as users


class AssessmentInvalid(ValueError):
    """No complete, reproducible assessment is available. Contains no records."""


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
    if inputs.fhe_qualified and not inputs.fhe_forfeited:
        if inputs.subsidy_covered_fees:
            covered_set = {c.strip().upper() for c in inputs.subsidy_covered_fees}
            aliased = set(covered_set)
            if "LABORATORY" in aliased or "LAB_FEE" in aliased:
                aliased.add("LAB")
            if "TUITION_FEE" in aliased:
                aliased.add("TUITION")
            if "REGISTRATION_FEE" in aliased:
                aliased.add("REGISTRATION")
            if "MISC_FEE" in aliased or "MISCELLANEOUS" in aliased:
                aliased.add("MISC")
            subsidy = sum(
                (Decimal(line.amount) for line in lines if line.code in aliased),
                Decimal(0),
            )
        else:
            subsidy = total
    else:
        subsidy = Decimal(0)
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
    await repository.store_assessment(db, stored, due_date)
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
