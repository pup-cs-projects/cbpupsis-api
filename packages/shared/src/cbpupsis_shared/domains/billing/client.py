"""Internal client for the billing domain: the extraction swap point."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.billing import service
from cbpupsis_shared.domains.billing.assessment import (
    AssessmentCourse,
    AssessmentInputs,
    AssessmentInvalid,
    AssessmentLine,
    AssessmentRead,
    StoredAssessment,
    calculate_assessment,
    read_stored_assessment,
    stage_assessment,
)
from cbpupsis_shared.domains.billing.exceptions import (
    IdempotencyKeyRequiredError,
    PaymentReceiptUnavailableError,
    WebhookSignatureInvalidError,
)
from cbpupsis_shared.domains.billing.provider import (
    GatewayCreateResult,
    GatewayRejected,
    GatewayState,
    GatewayStatusResult,
    GatewayTimedOut,
    PaymentGateway,
    XenditGateway,
)
from cbpupsis_shared.domains.billing.schemas import (
    HoldRead,
    PaymentCreate,
    PaymentCreated,
    PaymentRead,
    ReconciliationResult,
    StudentBillingAccountRead,
    XenditWebhook,
)

__all__ = [
    "AssessmentCourse",
    "AssessmentInputs",
    "AssessmentInvalid",
    "AssessmentLine",
    "AssessmentRead",
    "GatewayCreateResult",
    "GatewayRejected",
    "GatewayState",
    "GatewayStatusResult",
    "GatewayTimedOut",
    "HoldRead",
    "IdempotencyKeyRequiredError",
    "PaymentCreate",
    "PaymentCreated",
    "PaymentGateway",
    "PaymentRead",
    "PaymentReceiptUnavailableError",
    "ReconciliationResult",
    "StoredAssessment",
    "StudentBillingAccountRead",
    "WebhookSignatureInvalidError",
    "XenditGateway",
    "XenditWebhook",
    "calculate_assessment",
    "create_payment",
    "get_receipt_payment",
    "get_student_billing",
    "get_student_holds",
    "get_total_outstanding_balance",
    "process_xendit_webhook",
    "read_payment",
    "read_stored_assessment",
    "reconcile_payments",
    "stage_assessment",
    "valid_webhook_token",
    "webhook_timestamp_is_fresh",
]


async def get_student_billing(
    db: AsyncSession, student_id: uuid.UUID, term_id: uuid.UUID
) -> StudentBillingAccountRead | None:
    return await service.get_student_billing(db, student_id, term_id)


async def get_total_outstanding_balance(
    db: AsyncSession, student_id: uuid.UUID
) -> Decimal:
    return await service.get_total_outstanding_balance(db, student_id)


async def get_student_holds(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    has_financial_hold: bool = False,
    has_academic_hold: bool = False,
) -> list[HoldRead]:
    return await service.get_student_holds(
        db,
        student_id=student_id,
        has_financial_hold=has_financial_hold,
        has_academic_hold=has_academic_hold,
    )


create_payment = service.create_payment
get_receipt_payment = service.get_receipt_payment
process_xendit_webhook = service.process_xendit_webhook
read_payment = service.read_payment
reconcile_payments = service.reconcile_payments
valid_webhook_token = service.valid_webhook_token
webhook_timestamp_is_fresh = service.webhook_timestamp_is_fresh
