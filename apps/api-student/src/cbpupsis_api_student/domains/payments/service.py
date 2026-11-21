"""Student-facing facade over the shared billing payment service."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.payments.exports import receipt_pdf
from cbpupsis_shared.domains.billing import client as billing_client
from cbpupsis_shared.domains.billing.constants import PAYMENT_PAID

_PDF_SLOTS = asyncio.Semaphore(2)


async def create_payment(
    db: AsyncSession,
    *,
    student_id: uuid.UUID,
    customer_email: str,
    idempotency_key: str | None,
    amount: Decimal,
    client_ip: str | None,
    request_id: str | None,
):
    gateway = billing_client.XenditGateway()
    return await billing_client.create_payment(
        db,
        student_id=student_id,
        customer_email=customer_email,
        idempotency_key=idempotency_key,
        amount=amount,
        client_ip=client_ip,
        request_id=request_id,
        gateway=gateway,
    )


async def read_payment(
    db: AsyncSession, *, student_id: uuid.UUID, payment_id: uuid.UUID
):
    return await billing_client.read_payment(
        db, student_id=student_id, payment_id=payment_id
    )


async def export_receipt(
    db: AsyncSession, *, student_id: uuid.UUID, payment_id: uuid.UUID
) -> bytes:
    payment = await billing_client.get_receipt_payment(
        db, student_id=student_id, payment_id=payment_id
    )
    if payment.payment_status != PAYMENT_PAID:
        raise billing_client.PaymentReceiptUnavailableError
    snapshot = SimpleNamespace(
        id=payment.id,
        receipt_number=payment.receipt_number,
        amount=payment.amount,
        payment_status=payment.payment_status,
        gateway_reference=payment.gateway_reference,
        paid_at=payment.paid_at,
    )
    await db.rollback()
    async with _PDF_SLOTS:
        async with asyncio.timeout(10):
            return await asyncio.to_thread(receipt_pdf, snapshot)
