"""Business logic for the billing domain."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.billing import FeeStructure
from cbpupsis_shared.domains.academics import client as academics_client
from cbpupsis_shared.domains.audit import client as audit_client
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.billing import repository
from cbpupsis_shared.domains.billing.assessment import refresh_current_subsidy
from cbpupsis_shared.domains.billing.exceptions import (
    FeeStructureInUseError,
    FeeValidationError,
    InstallmentDateOrderError,
    InstallmentSumInvalidError,
)
from cbpupsis_shared.domains.billing.overdue import stage_overdue
from cbpupsis_shared.domains.billing.schemas import (
    FeeStructureRead,
    FeeStructureSaveRequest,
    HoldRead,
    InstallmentItemRead,
    ProgramTuitionRateRead,
    StudentBillingAccountRead,
)
from cbpupsis_shared.domains.users import client as users_client


async def get_student_billing(
    db: AsyncSession, student_id: uuid.UUID, term_id: uuid.UUID
) -> StudentBillingAccountRead | None:
    row = await repository.get_student_billing_account(db, student_id, term_id)
    if row is None:
        return None
    return StudentBillingAccountRead.model_validate(row)


async def get_total_outstanding_balance(
    db: AsyncSession, student_id: uuid.UUID
) -> Decimal:
    return await repository.get_total_outstanding_balance(db, student_id)


async def get_student_holds(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    has_financial_hold: bool = False,
    has_academic_hold: bool = False,
    as_of: datetime | None = None,
    reconcile_financials: bool = True,
) -> list[HoldRead]:
    """Use ledger debt, not a stale profile flag, for financial holds.

    Current-term subsidy reconciliation is staged on the caller's transaction.
    Explicit nonfinancial holds remain unchanged; the legacy financial flag is
    accepted for client compatibility but cannot invent or clear monetary debt.
    """
    if reconcile_financials:
        term = await academics_client.get_current_term(db)
        profile = await users_client.get_student_profile(db, student_id)
        if term is not None and profile is not None:
            assessment = await refresh_current_subsidy(db, profile, term.id)
            if assessment is not None:
                account = await repository.get_student_billing_account(
                    db, student_id, term.id, for_update=True
                )
                await stage_overdue(db, account, as_of=as_of)
    stored = await repository.list_unresolved_student_holds(db, student_id)
    outstanding = await repository.get_total_outstanding_balance(db, student_id)
    holds = [
        HoldRead(
            type=row.hold_type,
            office=row.office,
            description=row.description,
            amount_outstanding=f"{(row.amount_outstanding or Decimal('0')):.2f}",
            currency=row.currency,
            is_blocking=row.blocks_enrollment,
            blocks=["ENROLLMENT"] if row.blocks_enrollment else [],
            blocks_enrollment=row.blocks_enrollment,
        )
        for row in stored
        if row.hold_type.strip().upper() != "FINANCIAL"
    ]
    stored_types = {hold.type.strip().upper() for hold in holds}

    if outstanding > 0:
        financial_metadata = next(
            (row for row in stored if row.hold_type.strip().upper() == "FINANCIAL"),
            None,
        )
        holds.append(
            HoldRead(
                type="FINANCIAL",
                office=financial_metadata.office
                if financial_metadata
                else "Accounting Office",
                description=financial_metadata.description
                if financial_metadata
                else "Outstanding student account balance",
                amount_outstanding=f"{outstanding:.2f}",
                currency="PHP",
                is_blocking=True,
                blocks=["ENROLLMENT", "COR"],
                blocks_enrollment=True,
            )
        )

    if has_academic_hold and not stored_types.intersection(
        {"ACADEMIC", "ADMINISTRATIVE", "DISCIPLINARY"}
    ):
        holds.append(
            HoldRead(
                type="ACADEMIC",
                office="Registrar's Office",
                description="Academic document clearance hold",
                amount_outstanding="0.00",
                currency="PHP",
                is_blocking=True,
                blocks=["ENROLLMENT"],
                blocks_enrollment=True,
            )
        )

    return holds


def _format_installments(raw_list: list[dict] | None) -> list[InstallmentItemRead]:
    if not raw_list:
        return []
    res = []
    for item in raw_list:
        num = int(item.get("installment_number") or item.get("installmentNumber") or 1)
        pct = Decimal(str(item.get("percentage") or item.get("percent") or 0))
        d = item.get("due_date") or item.get("dueDate")
        if isinstance(d, str):
            d = date.fromisoformat(d)
        res.append(
            InstallmentItemRead(
                installment_number=num,
                percentage=f"{pct:.2f}",
                due_date=d,
            )
        )
    return res


def _build_read_dto(
    term_id: uuid.UUID,
    base_row: FeeStructure | None,
    program_rows: list[FeeStructure],
) -> FeeStructureRead:
    if base_row is None and not program_rows:
        return FeeStructureRead(
            academic_term_id=term_id,
            tuition_per_unit="0.00",
            program_tuition_rates=[],
            registration_fee="0.00",
            lab_fee="0.00",
            misc_fee="0.00",
            late_fee_rate="0.00",
            due_date=None,
            subsidy_covered_fees=[],
            installments=[],
        )
    row = base_row if base_row is not None else program_rows[0]
    p_rates = [
        ProgramTuitionRateRead(
            program_id=p.program_id,  # type: ignore[arg-type]
            tuition_per_unit=f"{Decimal(str(p.tuition_per_unit)):.2f}",
        )
        for p in program_rows
        if p.program_id is not None
    ]
    raw_inst = row.installments if row.installments else []
    return FeeStructureRead(
        academic_term_id=term_id,
        tuition_per_unit=f"{Decimal(str(row.tuition_per_unit)):.2f}",
        program_tuition_rates=p_rates,
        registration_fee=f"{Decimal(str(row.registration_fee)):.2f}",
        lab_fee=f"{Decimal(str(row.lab_fee)):.2f}",
        misc_fee=f"{Decimal(str(row.misc_fee)):.2f}",
        late_fee_rate=f"{Decimal(str(row.late_fee_rate)):.2f}",
        due_date=row.due_date,
        subsidy_covered_fees=row.subsidy_covered_fees or [],
        installments=_format_installments(raw_inst),
    )


async def get_fee_structure_for_term(
    db: AsyncSession, term_id: uuid.UUID
) -> FeeStructureRead:
    """Read a term's fee structure or return baseline default if none saved."""
    term = await academics_client.get_academic_term(db, term_id)
    if term is None:
        raise ResourceNotFoundError(f"Academic term {term_id} not found.")
    existing_rows = await repository.get_fee_structures_for_term(db, term_id)
    base_row = next((r for r in existing_rows if r.program_id is None), None)
    program_rows = [r for r in existing_rows if r.program_id is not None]
    return _build_read_dto(term_id, base_row, program_rows)


async def save_fee_structure(
    db: AsyncSession,
    term_id: uuid.UUID,
    payload: FeeStructureSaveRequest,
    *,
    actor_id: uuid.UUID,
    role: str = "finance",
    client_ip: str | None = None,
) -> FeeStructureRead:
    """Save and validate fee structure for term, enforcing audit and immutability."""
    term = await academics_client.get_academic_term(db, term_id)
    if term is None:
        raise ResourceNotFoundError(f"Academic term {term_id} not found.")

    if await repository.count_term_assessments(db, term_id) > 0:
        raise FeeStructureInUseError()

    if payload.tuition_per_unit < Decimal("0"):
        raise FeeValidationError("tuition_per_unit")
    if payload.registration_fee < Decimal("0"):
        raise FeeValidationError("registration_fee")
    if payload.lab_fee < Decimal("0"):
        raise FeeValidationError("lab_fee")
    if payload.misc_fee < Decimal("0"):
        raise FeeValidationError("misc_fee")
    if payload.late_fee_rate < Decimal("0"):
        raise FeeValidationError("late_fee_rate")
    for pr in payload.program_tuition_rates:
        if pr.tuition_per_unit < Decimal("0"):
            raise FeeValidationError("tuition_per_unit")

    if payload.installments:
        total_pct = sum(
            (Decimal(str(item.percentage)) for item in payload.installments),
            Decimal(0),
        )
        if total_pct != Decimal("100"):
            raise InstallmentSumInvalidError(total_pct)
        for i in range(1, len(payload.installments)):
            prev_d = payload.installments[i - 1].due_date
            curr_d = payload.installments[i].due_date
            if curr_d < prev_d:
                raise InstallmentDateOrderError(prev_d, curr_d)

    existing_rows = await repository.get_fee_structures_for_term(db, term_id)
    prior_state = (
        [
            {
                "program_id": str(r.program_id) if r.program_id else None,
                "tuition_per_unit": str(r.tuition_per_unit),
                "registration_fee": str(r.registration_fee),
                "lab_fee": str(r.lab_fee),
                "misc_fee": str(r.misc_fee),
                "late_fee_rate": str(r.late_fee_rate),
                "due_date": r.due_date.isoformat() if r.due_date else None,
                "subsidy_covered_fees": r.subsidy_covered_fees,
                "installments": r.installments,
            }
            for r in existing_rows
        ]
        if existing_rows
        else None
    )

    inst_data = [item.model_dump(mode="json") for item in payload.installments]
    rows_by_program = {r.program_id: r for r in existing_rows}

    base_row = rows_by_program.get(None)
    if base_row is None:
        base_row = FeeStructure(
            academic_term_id=term_id,
            program_id=None,
        )
        db.add(base_row)
    base_row.tuition_per_unit = payload.tuition_per_unit
    base_row.registration_fee = payload.registration_fee
    base_row.lab_fee = payload.lab_fee
    base_row.misc_fee = payload.misc_fee
    base_row.late_fee_rate = payload.late_fee_rate
    base_row.due_date = payload.due_date
    base_row.subsidy_covered_fees = payload.subsidy_covered_fees
    base_row.installments = inst_data

    program_rows = []
    for pr in payload.program_tuition_rates:
        p_row = rows_by_program.get(pr.program_id)
        if p_row is None:
            p_row = FeeStructure(
                academic_term_id=term_id,
                program_id=pr.program_id,
            )
            db.add(p_row)
        p_row.tuition_per_unit = pr.tuition_per_unit
        p_row.registration_fee = payload.registration_fee
        p_row.lab_fee = payload.lab_fee
        p_row.misc_fee = payload.misc_fee
        p_row.late_fee_rate = payload.late_fee_rate
        p_row.due_date = payload.due_date
        p_row.subsidy_covered_fees = payload.subsidy_covered_fees
        p_row.installments = inst_data
        program_rows.append(p_row)

    await db.flush()

    new_state = payload.model_dump(mode="json")
    audit_client.stage(
        db,
        action="billing.fee_structure.saved",
        actor_id=actor_id,
        target_type="academic_term",
        target_id=str(term_id),
        payload={"role": role, "client_ip": client_ip, "timezone": "UTC+8"},
        prior_state=prior_state,
        new_state=new_state,
    )
    await db.commit()

    return _build_read_dto(term_id, base_row, program_rows)
