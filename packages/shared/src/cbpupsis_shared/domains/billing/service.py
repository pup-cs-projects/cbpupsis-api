"""Business logic for the billing domain."""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_shared.domains.audit import client as audit_client
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.billing import repository
from cbpupsis_shared.domains.billing.constants import (
    IDEMPOTENCY_COMPLETED,
    IDEMPOTENCY_PROCESSING,
    PAYMENT_FAILED,
    PAYMENT_PAID,
    PAYMENT_PENDING,
)
from cbpupsis_shared.domains.billing.exceptions import (
    AmountExceedsBalanceError,
    IdempotencyKeyRequiredError,
    PaymentInProgressError,
)
from cbpupsis_shared.domains.billing.provider import (
    GatewayRejected,
    GatewayState,
    GatewayTimedOut,
    PaymentGateway,
)
from cbpupsis_shared.domains.billing.schemas import (
    HoldRead,
    PaymentRead,
    ReconciliationResult,
    StudentBillingAccountRead,
    XenditWebhook,
)

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
) -> list[HoldRead]:
    """Assemble all blocking and non-blocking holds for the student."""
    stored = await repository.list_unresolved_student_holds(db, student_id)
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
    ]
    stored_types = {hold.type.strip().upper() for hold in holds}

    if has_financial_hold and "FINANCIAL" not in stored_types:
        outstanding = await repository.get_total_outstanding_balance(db, student_id)
        holds.append(
            HoldRead(
                type="FINANCIAL",
                office="Accounting Office",
                description="Outstanding balance from previous term",
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
) -> PaymentRead:
    payment = await repository.get_student_payment(db, student_id, payment_id)
    if payment is None:
        raise ResourceNotFoundError
    return PaymentRead(
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
