"""Exactly-once student payment and authenticated Xendit webhook routes."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.payments import service
from cbpupsis_api_student.domains.payments.schemas import (
    PaymentCreate,
    PaymentCreated,
    PaymentStatusRead,
    XenditWebhook,
)
from cbpupsis_api_student.domains.student_auth.dependencies import (
    require_student_session,
)
from cbpupsis_core.exceptions import AppError
from cbpupsis_core.middleware import client_ip_or_none
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser
from cbpupsis_shared.domains.billing import client as billing_client
from cbpupsis_shared.domains.billing.constants import CREATE_PAYMENT, READ_PAYMENT
from cbpupsis_shared.domains.iam.dependencies import require_permission

logger = logging.getLogger(__name__)
Database = Annotated[AsyncSession, Depends(get_db)]
Student = Annotated[CurrentUser, Depends(require_student_session)]


async def private_record(request: Request) -> None:
    request.state.private_response = True


async def required_idempotency_key(request: Request) -> str:
    value = request.headers.get("Idempotency-Key", "").strip()
    if not value:
        raise billing_client.IdempotencyKeyRequiredError
    if len(value) > 255:
        raise AppError("Idempotency-Key must be no longer than 255 characters.")
    return value


router = APIRouter(
    dependencies=[Depends(private_record), Depends(require_student_session)],
    responses={
        401: {"description": "A live student session is required."},
        403: {"description": "The required payment permission is missing."},
        404: {"description": "RESOURCE_NOT_FOUND"},
    },
)
public_router = APIRouter()


@router.post(
    "",
    status_code=201,
    response_model=PaymentCreated,
    dependencies=[Depends(require_permission(CREATE_PAYMENT))],
    openapi_extra={
        "parameters": [
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "minLength": 1, "maxLength": 255},
                "description": "Unique retry key scoped to the signed-in student.",
            }
        ]
    },
    responses={
        201: {"description": "Hosted Xendit checkout created exactly once."},
        400: {"description": "IDEMPOTENCY_KEY_REQUIRED"},
        409: {"description": "PAYMENT_IN_PROGRESS"},
        422: {"description": "AMOUNT_EXCEEDS_BALANCE"},
        502: {"description": "PAYMENT_GATEWAY_ERROR"},
        504: {"description": "PAYMENT_GATEWAY_TIMEOUT"},
    },
)
async def create_payment(
    data: PaymentCreate,
    request: Request,
    user: Student,
    db: Database,
    idempotency_key: Annotated[str, Depends(required_idempotency_key)],
) -> JSONResponse:
    """Create one hosted checkout; the student/key pair is the replay identity."""
    result = await service.create_payment(
        db,
        student_id=user.id,
        customer_email=user.email,
        idempotency_key=idempotency_key,
        amount=data.amount,
        client_ip=client_ip_or_none(request),
        request_id=getattr(request.state, "request_id", None),
    )
    return JSONResponse(status_code=result.status_code, content=result.body)


@router.get(
    "/{payment_id}",
    response_model=PaymentStatusRead,
    dependencies=[Depends(require_permission(READ_PAYMENT))],
)
async def read_payment(
    payment_id: uuid.UUID, user: Student, db: Database
) -> PaymentStatusRead:
    """Read only the signed-in student's payment; foreign IDs are hidden as 404."""
    return await service.read_payment(db, student_id=user.id, payment_id=payment_id)


@router.get(
    "/{payment_id}/receipt",
    response_class=Response,
    dependencies=[Depends(require_permission(READ_PAYMENT))],
    responses={200: {"content": {"application/pdf": {}}}},
)
async def payment_receipt(
    payment_id: uuid.UUID, user: Student, db: Database
) -> Response:
    """Download a settled, card-data-free payment receipt PDF."""
    content = await service.export_receipt(
        db, student_id=user.id, payment_id=payment_id
    )
    return Response(
        content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="payment-receipt-{payment_id}.pdf"'
            ),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@public_router.post(
    "/webhooks/xendit",
    responses={401: {"description": "WEBHOOK_SIGNATURE_INVALID"}},
)
async def xendit_webhook(
    request: Request,
    db: Database,
    callback_token: Annotated[str | None, Header(alias="x-callback-token")] = None,
    webhook_id: Annotated[str | None, Header(alias="webhook-id")] = None,
) -> dict[str, bool]:
    """Verify Xendit's token before parsing or touching payment state."""
    if not billing_client.valid_webhook_token(callback_token) or not webhook_id:
        logger.warning("payment.webhook_signature_invalid")
        raise billing_client.WebhookSignatureInvalidError

    try:
        raw = await request.body()
        if len(raw) > 64 * 1024:
            raise ValueError
        webhook = XenditWebhook.model_validate_json(raw)
    except (ValueError, json.JSONDecodeError, PydanticValidationError):
        raise AppError("The webhook payload is invalid.") from None
    if not billing_client.webhook_timestamp_is_fresh(webhook.created):
        logger.warning("payment.webhook_timestamp_invalid")
        raise billing_client.WebhookSignatureInvalidError

    processed = await billing_client.process_xendit_webhook(
        db, webhook_id=webhook_id, webhook=webhook
    )
    return {"received": True, "duplicate": not processed}
