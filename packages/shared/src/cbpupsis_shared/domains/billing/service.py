"""Business logic for the billing domain."""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_database.models.billing import FeeStructure
from cbpupsis_shared.domains.academics import client as academics_client
from cbpupsis_shared.domains.audit import client as audit_client
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.billing import repository
from cbpupsis_shared.domains.billing.assessment import refresh_current_subsidy
from cbpupsis_shared.domains.billing.constants import (
    IDEMPOTENCY_COMPLETED,
    IDEMPOTENCY_PROCESSING,
    PAYMENT_FAILED,
    PAYMENT_PAID,
    PAYMENT_PENDING,
)
from cbpupsis_shared.domains.billing.exceptions import (
    AmountExceedsBalanceError,
    FeeStructureInUseError,
    FeeValidationError,
    IdempotencyKeyRequiredError,
    InstallmentDateOrderError,
    InstallmentSumInvalidError,
    PaymentInProgressError,
)
from cbpupsis_shared.domains.billing.overdue import stage_overdue
from cbpupsis_shared.domains.billing.provider import (
    GatewayRejected,
    GatewayState,
    GatewayTimedOut,
    PaymentGateway,
)
from cbpupsis_shared.domains.billing.schemas import (
    FeeStructureRead,
    FeeStructureSaveRequest,
    HoldRead,
    InstallmentItemRead,
    PaymentStatusRead,
    ProgramTuitionRateRead,
    ReconciliationResult,
    StudentBillingAccountRead,
    XenditWebhook,
)
from cbpupsis_shared.domains.users import client as users_client

logger = logging.getLogger(__name__)
MANILA = ZoneInfo("Asia/Manila")


@dataclass(frozen=True)
class PaymentHttpResult:
    status_code: int
    body: dict


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


def _error_body(
    *, request_id: str | None, code: str, detail: str, reason: str | None = None
) -> dict:
    body = {
        "status": "error",
        "message": detail,
        "detail": detail,
        "request_id": request_id,
        "code": code,
    }
    if reason is not None:
        body["reason"] = reason
    return body


def _saved_result(payment) -> PaymentHttpResult:
    if payment.idempotency_status == IDEMPOTENCY_PROCESSING:
        raise PaymentInProgressError(settings.payment_retry_after_seconds)
    if payment.response_status is None or payment.response_body is None:
        raise PaymentInProgressError(settings.payment_retry_after_seconds)
    return PaymentHttpResult(payment.response_status, dict(payment.response_body))


async def create_payment(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    customer_email: str,
    idempotency_key: str | None,
    amount: Decimal,
    client_ip: str | None,
    request_id: str | None,
    gateway: PaymentGateway,
) -> PaymentHttpResult:
    """Create one durable gateway checkout for one student/key pair."""
    key = (idempotency_key or "").strip()
    if not key:
        raise IdempotencyKeyRequiredError

    existing = await repository.get_payment_by_idempotency(db, student_id, key)
    if existing is not None:
        return _saved_result(existing)

    account = await repository.lock_outstanding_account(db, student_id)
    if account is None:
        raise AmountExceedsBalanceError
    existing = await repository.get_payment_by_idempotency(db, student_id, key)
    if existing is not None:
        return _saved_result(existing)

    reserved = await repository.pending_reservations(db, account.id)
    balance = Decimal(str(account.balance or Decimal("0.00")))
    if amount > balance - reserved:
        raise AmountExceedsBalanceError

    payment_id = uuid.uuid4()
    merchant_reference = f"cbp-{payment_id.hex}"
    payment = repository.stage_payment(
        db,
        payment_id=payment_id,
        billing_account_id=account.id,
        student_id=student_id,
        idempotency_key=key,
        merchant_reference=merchant_reference,
        amount=amount,
        initiated_ip=client_ip,
    )
    try:
        # The attempt must be visible before the network call. A crash or timeout
        # then leaves a row reconciliation can recover rather than a second charge.
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await repository.get_payment_by_idempotency(db, student_id, key)
        if existing is None:
            raise
        return _saved_result(existing)

    try:
        created = await gateway.create_session(
            merchant_reference=merchant_reference,
            amount=amount,
            customer_email=customer_email,
        )
    except GatewayTimedOut:
        body = _error_body(
            request_id=request_id,
            code="PAYMENT_GATEWAY_TIMEOUT",
            detail=(
                "The payment gateway did not respond in time. "
                "The attempt remains pending."
            ),
        )
        payment.idempotency_status = IDEMPOTENCY_COMPLETED
        payment.response_status = 504
        payment.response_body = body
        await db.commit()
        return PaymentHttpResult(504, body)
    except GatewayRejected as exc:
        body = _error_body(
            request_id=request_id,
            code="PAYMENT_GATEWAY_ERROR",
            detail="The payment gateway rejected the request.",
            reason=exc.reason,
        )
        payment.payment_status = PAYMENT_FAILED
        payment.failure_reason = exc.reason
        payment.idempotency_status = IDEMPOTENCY_COMPLETED
        payment.response_status = 502
        payment.response_body = body
        await db.commit()
        return PaymentHttpResult(502, body)

    body = {
        "paymentId": str(payment.id),
        "state": PAYMENT_PENDING,
        "checkoutUrl": created.checkout_url,
    }
    payment.gateway_session_id = created.session_id
    payment.gateway_checkout_url = created.checkout_url
    payment.idempotency_status = IDEMPOTENCY_COMPLETED
    payment.response_status = 201
    payment.response_body = body
    await db.commit()
    return PaymentHttpResult(201, body)


async def read_payment(
    db: AsyncSession, *, student_id: uuid.UUID, payment_id: uuid.UUID
) -> PaymentStatusRead:
    payment = await repository.get_student_payment(db, student_id, payment_id)
    if payment is None:
        raise ResourceNotFoundError
    return PaymentStatusRead(
        paymentId=payment.id,
        state=payment.payment_status,
        amount=f"{payment.amount:.2f}",
        gatewayReference=payment.gateway_reference,
        receiptNumber=payment.receipt_number,
        paidAt=payment.paid_at,
        createdAt=payment.created_at,
    )


async def get_receipt_payment(
    db: AsyncSession, *, student_id: uuid.UUID, payment_id: uuid.UUID
):
    payment = await repository.get_student_payment(db, student_id, payment_id)
    if payment is None:
        raise ResourceNotFoundError
    return payment


async def _settle_locked(db: AsyncSession, payment, *, source: str) -> bool:
    if payment.payment_status == PAYMENT_PAID:
        return False
    account = await repository.lock_billing_account(db, payment.billing_account_id)
    if account is None:
        raise RuntimeError("Payment account is missing")
    old_balance = Decimal(str(account.balance or Decimal("0.00")))
    old_paid = Decimal(str(account.amount_paid))
    new_paid = old_paid + Decimal(str(payment.amount))
    net_due = Decimal(str(account.net_amount_due or 0))
    account.amount_paid = new_paid
    account.payment_status = "paid" if new_paid >= net_due else "partial"
    payment.payment_status = PAYMENT_PAID
    payment.paid_at = datetime.now(UTC)
    payment.receipt_number = payment.receipt_number or (
        f"R-{payment.id.hex[:12].upper()}"
    )
    occurred = datetime.now(MANILA)
    audit_client.stage(
        db,
        action="billing.payment_settled",
        actor_id=payment.student_id,
        target_type="payment",
        target_id=payment.id,
        occurred_at=occurred,
        prior_state={"balance": f"{old_balance:.2f}"},
        new_state={"balance": f"{old_balance - payment.amount:.2f}"},
        payload={
            "gatewayReference": payment.gateway_reference,
            "amount": f"{payment.amount:.2f}",
            "actor": source,
            "timestamp": occurred.isoformat(),
            "clientIp": payment.initiated_ip,
        },
    )
    return True


async def settle_payment(
    db: AsyncSession,
    *,
    payment_id: uuid.UUID,
    gateway_reference: str,
    source: str,
) -> bool:
    payment = await repository.lock_payment(db, payment_id)
    if payment is None:
        return False
    if payment.gateway_reference is None:
        payment.gateway_reference = gateway_reference
    changed = await _settle_locked(db, payment, source=source)
    await db.commit()
    return changed


async def fail_payment(db: AsyncSession, *, payment_id: uuid.UUID, reason: str) -> bool:
    payment = await repository.lock_payment(db, payment_id)
    if payment is None or payment.payment_status != PAYMENT_PENDING:
        return False
    payment.payment_status = PAYMENT_FAILED
    payment.failure_reason = reason[:255]
    await db.commit()
    return True


def valid_webhook_token(presented: str | None) -> bool:
    configured = settings.xendit_webhook_verification_token
    if configured is None or presented is None:
        return False
    return secrets.compare_digest(configured.get_secret_value(), presented)


def webhook_timestamp_is_fresh(
    created: datetime, *, now: datetime | None = None
) -> bool:
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return abs((current - created.astimezone(UTC)).total_seconds()) <= (
        settings.xendit_webhook_tolerance_seconds
    )


async def process_xendit_webhook(
    db: AsyncSession, *, webhook_id: str, webhook: XenditWebhook
) -> bool:
    if await repository.get_webhook_event(db, webhook_id) is not None:
        logger.info("payment.webhook_duplicate", extra={"webhook_id": webhook_id})
        return False
    payment = await repository.lock_payment_by_merchant_reference(
        db, webhook.data.reference_id
    )
    if payment is None:
        logger.warning("payment.webhook_unknown_reference")
        return False
    if await repository.get_webhook_event(db, webhook_id) is not None:
        logger.info("payment.webhook_duplicate", extra={"webhook_id": webhook_id})
        return False

    if webhook.event == "payment_session.completed":
        payment.gateway_session_id = (
            webhook.data.payment_session_id or payment.gateway_session_id
        )
        payment.gateway_reference = (
            webhook.data.payment_id
            or payment.gateway_reference
            or webhook.data.payment_session_id
        )
        await _settle_locked(db, payment, source="xendit_webhook")
    elif webhook.event == "payment_session.expired":
        if payment.payment_status == PAYMENT_PENDING:
            payment.payment_status = PAYMENT_FAILED
            payment.failure_reason = "Xendit payment session expired"
    else:
        logger.info("payment.webhook_ignored", extra={"event": webhook.event})

    repository.stage_webhook_event(
        db,
        webhook_id=webhook_id,
        payment_id=payment.id,
        event_type=webhook.event,
        event_created_at=webhook.created,
    )
    await db.commit()
    return True


async def reconcile_payments(
    db: AsyncSession,
    *,
    run_key: str,
    gateway: PaymentGateway,
    limit: int = 100,
) -> ReconciliationResult:
    existing = await repository.get_reconciliation_run(db, run_key)
    if existing is not None and existing.status == "COMPLETED":
        return ReconciliationResult(
            runKey=run_key,
            processed=existing.processed_count,
            settled=existing.settled_count,
            failed=existing.failed_count,
        )
    run = existing or repository.stage_reconciliation_run(db, run_key)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        run = await repository.get_reconciliation_run(db, run_key)
        if run is None:
            raise
        if run.status == "COMPLETED":
            return ReconciliationResult(
                runKey=run_key,
                processed=run.processed_count,
                settled=run.settled_count,
                failed=run.failed_count,
            )

    cutoff = datetime.now(UTC) - timedelta(
        seconds=settings.payment_reconciliation_pending_seconds
    )
    pending = await repository.list_stale_pending_payments(db, cutoff, limit)
    identifiers = [
        (row.id, row.merchant_reference, row.gateway_session_id) for row in pending
    ]
    await db.rollback()
    processed = settled = failed = 0
    for payment_id, merchant_reference, session_id in identifiers:
        try:
            status = await gateway.get_status(
                merchant_reference=merchant_reference, session_id=session_id
            )
        except (GatewayRejected, GatewayTimedOut):
            logger.warning(
                "payment.reconciliation_gateway_unavailable",
                extra={"payment_id": str(payment_id)},
            )
            continue
        processed += 1
        if status.state == GatewayState.PAID:
            changed = await settle_payment(
                db,
                payment_id=payment_id,
                gateway_reference=status.gateway_reference or merchant_reference,
                source="reconciliation",
            )
            settled += int(changed)
        elif status.state == GatewayState.FAILED:
            changed = await fail_payment(
                db,
                payment_id=payment_id,
                reason=status.reason or "Xendit marked the payment failed",
            )
            failed += int(changed)

    run = await repository.get_reconciliation_run(db, run_key)
    if run is None:
        raise RuntimeError("Reconciliation run marker is missing")
    run.status = "COMPLETED"
    run.processed_count = processed
    run.settled_count = settled
    run.failed_count = failed
    run.completed_at = datetime.now(UTC)
    await db.commit()
    return ReconciliationResult(
        runKey=run_key, processed=processed, settled=settled, failed=failed
    )
