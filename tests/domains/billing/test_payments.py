"""US-FIN-002 acceptance coverage without a live database or gateway."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cbpupsis_api_student.domains.payments.exports import receipt_pdf
from cbpupsis_api_student.main import app as student_app
from cbpupsis_core.config import settings
from cbpupsis_database.base import Base
from cbpupsis_database.models.academics import AcademicTerm
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.billing import (
    Payment,
    PaymentWebhookEvent,
    StudentBillingAccount,
)
from cbpupsis_database.models.users import User
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.billing import service
from cbpupsis_shared.domains.billing.constants import PAYMENT_PAID, PAYMENT_PENDING
from cbpupsis_shared.domains.billing.exceptions import (
    AmountExceedsBalanceError,
    IdempotencyKeyRequiredError,
    PaymentInProgressError,
)
from cbpupsis_shared.domains.billing.provider import (
    GatewayCreateResult,
    GatewayRejected,
    GatewayState,
    GatewayStatusResult,
    GatewayTimedOut,
)
from cbpupsis_shared.domains.billing.schemas import XenditWebhook


class FakeGateway:
    def __init__(self) -> None:
        self.create_calls = 0
        self.status_calls = 0
        self.create_error: Exception | None = None
        self.status = GatewayStatusResult(GatewayState.PENDING)
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def create_session(self, **_kwargs) -> GatewayCreateResult:
        self.create_calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.create_error is not None:
            raise self.create_error
        return GatewayCreateResult(
            session_id=f"session-{self.create_calls}",
            checkout_url=f"https://checkout.example/{self.create_calls}",
        )

    async def get_status(self, **_kwargs) -> GatewayStatusResult:
        self.status_calls += 1
        return self.status


async def _billing_account(
    db: AsyncSession, *, gross: Decimal = Decimal("1000.00")
) -> tuple[User, StudentBillingAccount]:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.com",
        password_hash="!fixture",
        full_name="Payment Student",
    )
    now = datetime.now(UTC)
    year = 2000 + uuid.uuid4().int % 5000
    term = AcademicTerm(
        id=uuid.uuid4(),
        academic_year=f"{year}-{year + 1}",
        semester="FIRST",
        term_type="REGULAR",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 5, 1),
        regular_enrollment_start=now,
        regular_enrollment_end=now,
        add_drop_deadline=now,
        grade_submission_deadline=now,
    )
    account = StudentBillingAccount(
        id=uuid.uuid4(),
        student_id=user.id,
        academic_term_id=term.id,
        gross_amount=gross,
        fhe_subsidy_amount=Decimal("0.00"),
        amount_paid=Decimal("0.00"),
        due_date=date(2026, 6, 1),
    )
    db.add_all([user, term, account])
    await db.commit()
    await db.refresh(account)
    return user, account


async def _create(db, user, gateway, *, key="key-1", amount="100.00"):
    return await service.create_payment(
        db,
        student_id=user.id,
        customer_email=user.email,
        idempotency_key=key,
        amount=Decimal(amount),
        client_ip="198.51.100.4",
        request_id="request-1",
        gateway=gateway,
    )


@pytest.mark.asyncio
async def test_ac_fin_002_1_creates_one_hosted_checkout(db):
    user, _account = await _billing_account(db)
    gateway = FakeGateway()

    result = await _create(db, user, gateway)

    assert result.status_code == 201
    assert result.body["state"] == PAYMENT_PENDING
    assert result.body["checkoutUrl"].startswith("https://checkout.example/")
    assert gateway.create_calls == 1
    stored = await db.scalar(select(Payment))
    assert stored.idempotency_key == "key-1"
    assert stored.amount == Decimal("100.00")


@pytest.mark.asyncio
async def test_ac_fin_002_2_requires_nonempty_idempotency_key(db):
    user, _account = await _billing_account(db)
    with pytest.raises(IdempotencyKeyRequiredError) as failure:
        await _create(db, user, FakeGateway(), key="  ")
    assert failure.value.code == "IDEMPOTENCY_KEY_REQUIRED"


@pytest.mark.asyncio
async def test_ac_fin_002_3_replays_finished_key_byte_for_byte(db):
    user, _account = await _billing_account(db)
    gateway = FakeGateway()
    first = await _create(db, user, gateway)
    second = await _create(db, user, gateway)
    assert second == first
    assert gateway.create_calls == 1


@pytest.mark.asyncio
async def test_ac_fin_002_4_concurrent_key_is_in_progress_exactly_once():
    database = Path.cwd() / f".test-payments-{uuid.uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as setup:
        user, _account = await _billing_account(setup, gross=Decimal("1000.00"))

    gateway = FakeGateway()
    for index in range(100):
        gateway.entered = asyncio.Event()
        gateway.release = asyncio.Event()
        async with sessions() as first_db, sessions() as rival_db:
            first = asyncio.create_task(
                _create(first_db, user, gateway, key=f"race-{index}", amount="1.00")
            )
            await asyncio.wait_for(gateway.entered.wait(), timeout=2)
            with pytest.raises(PaymentInProgressError):
                await _create(
                    rival_db, user, gateway, key=f"race-{index}", amount="1.00"
                )
            await rival_db.rollback()
            gateway.release.set()
            assert (await first).status_code == 201

    async with sessions() as verify:
        count = await verify.scalar(select(func.count()).select_from(Payment))
    assert count == 100
    assert gateway.create_calls == 100
    await engine.dispose()
    database.unlink()


@pytest.mark.asyncio
async def test_ac_fin_002_5_refuses_amount_above_unreserved_balance(db):
    user, _account = await _billing_account(db, gross=Decimal("50.00"))
    with pytest.raises(AmountExceedsBalanceError) as failure:
        await _create(db, user, FakeGateway(), amount="50.01")
    assert failure.value.code == "AMOUNT_EXCEEDS_BALANCE"


@pytest.mark.asyncio
async def test_ac_fin_002_6_gateway_rejection_is_stable_and_replay_safe(db):
    user, _account = await _billing_account(db)
    gateway = FakeGateway()
    gateway.create_error = GatewayRejected("CHANNEL_UNAVAILABLE")
    first = await _create(db, user, gateway)
    second = await _create(db, user, gateway)
    assert first == second
    assert first.status_code == 502
    assert first.body["code"] == "PAYMENT_GATEWAY_ERROR"
    assert first.body["reason"] == "CHANNEL_UNAVAILABLE"
    assert gateway.create_calls == 1


@pytest.mark.asyncio
async def test_ac_fin_002_7_timeout_stays_pending_and_replays_504(db):
    user, _account = await _billing_account(db)
    gateway = FakeGateway()
    gateway.create_error = GatewayTimedOut()
    first = await _create(db, user, gateway)
    second = await _create(db, user, gateway)
    stored = await db.scalar(select(Payment))
    assert first == second
    assert first.status_code == 504
    assert first.body["code"] == "PAYMENT_GATEWAY_TIMEOUT"
    assert stored.payment_status == PAYMENT_PENDING
    assert gateway.create_calls == 1


def test_ac_fin_002_8_rejects_bad_token_and_stale_webhook(monkeypatch):
    monkeypatch.setattr(
        settings, "xendit_webhook_verification_token", SecretStr("known-token")
    )
    assert service.valid_webhook_token("bad-token") is False
    assert service.valid_webhook_token("known-token") is True
    stale = datetime.now(UTC) - timedelta(
        seconds=settings.xendit_webhook_tolerance_seconds + 1
    )
    assert service.webhook_timestamp_is_fresh(stale) is False


@pytest.mark.asyncio
async def test_ac_fin_002_9_duplicate_webhook_settles_and_audits_once(db):
    user, account = await _billing_account(db)
    result = await _create(db, user, FakeGateway())
    payment_id = uuid.UUID(result.body["paymentId"])
    payment = await db.get(Payment, payment_id)
    webhook = XenditWebhook.model_validate(
        {
            "event": "payment_session.completed",
            "created": datetime.now(UTC),
            "data": {
                "reference_id": payment.merchant_reference,
                "payment_session_id": payment.gateway_session_id,
                "payment_id": "xendit-payment-1",
            },
        }
    )

    assert await service.process_xendit_webhook(
        db, webhook_id="webhook-1", webhook=webhook
    )
    assert not await service.process_xendit_webhook(
        db, webhook_id="webhook-1", webhook=webhook
    )
    await db.refresh(account)
    await db.refresh(payment)
    assert account.amount_paid == Decimal("100.00")
    assert payment.payment_status == PAYMENT_PAID
    assert await db.scalar(select(func.count()).select_from(PaymentWebhookEvent)) == 1
    audit = await db.scalar(select(AuditEntry))
    assert audit.payload["gatewayReference"] == "xendit-payment-1"
    assert audit.payload["amount"] == "100.00"
    assert audit.payload["actor"] == "xendit_webhook"
    assert audit.payload["timestamp"].endswith("+08:00")
    assert audit.payload["clientIp"] == "198.51.100.4"


@pytest.mark.asyncio
async def test_ac_fin_002_10_reconciliation_run_key_cannot_settle_twice(
    db, monkeypatch
):
    monkeypatch.setattr(settings, "payment_reconciliation_pending_seconds", 30)
    user, account = await _billing_account(db)
    create_gateway = FakeGateway()
    create_gateway.create_error = GatewayTimedOut()
    await _create(db, user, create_gateway)
    payment = await db.scalar(select(Payment))
    payment.created_at = datetime.now(UTC) - timedelta(minutes=5)
    await db.commit()
    gateway = FakeGateway()
    gateway.status = GatewayStatusResult(
        GatewayState.PAID, gateway_reference="reconciled-payment-1"
    )

    first = await service.reconcile_payments(
        db, run_key="eventbridge-1", gateway=gateway
    )
    second = await service.reconcile_payments(
        db, run_key="eventbridge-1", gateway=gateway
    )
    await db.refresh(account)
    assert first == second
    assert first.settled == 1
    assert account.amount_paid == Decimal("100.00")
    assert gateway.status_calls == 1


@pytest.mark.asyncio
async def test_ac_fin_002_receipt_is_pdf_and_foreign_ids_are_hidden(db):
    user, _account = await _billing_account(db)
    other = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.com",
        password_hash="!fixture",
    )
    db.add(other)
    await db.commit()
    result = await _create(db, user, FakeGateway())
    payment_id = uuid.UUID(result.body["paymentId"])
    await service.settle_payment(
        db,
        payment_id=payment_id,
        gateway_reference="paid-1",
        source="reconciliation",
    )
    payment = await service.get_receipt_payment(
        db, student_id=user.id, payment_id=payment_id
    )
    assert receipt_pdf(payment).startswith(b"%PDF-")
    with pytest.raises(ResourceNotFoundError):
        await service.read_payment(db, student_id=other.id, payment_id=payment_id)
    forbidden = {"card", "pan", "cvv", "expiry"}
    assert not any(
        word in column.name.lower()
        for column in Payment.__table__.columns
        for word in forbidden
    )


def test_ac_fin_002_openapi_documents_routes_header_and_six_codes():
    paths = student_app.openapi()["paths"]
    create = paths["/api/v1/student/payments"]["post"]
    header = next(
        parameter
        for parameter in create["parameters"]
        if parameter["name"] == "Idempotency-Key"
    )
    assert header["required"] is True
    assert create["responses"]["201"]["content"]["application/json"]["schema"]
    documented = {
        create["responses"][status]["description"]
        for status in ("400", "409", "422", "502", "504")
    }
    documented.add(
        paths["/api/v1/public/payments/webhooks/xendit"]["post"]["responses"]["401"][
            "description"
        ]
    )
    assert documented == {
        "IDEMPOTENCY_KEY_REQUIRED",
        "PAYMENT_IN_PROGRESS",
        "AMOUNT_EXCEEDS_BALANCE",
        "PAYMENT_GATEWAY_ERROR",
        "PAYMENT_GATEWAY_TIMEOUT",
        "WEBHOOK_SIGNATURE_INVALID",
    }
    assert "security" not in paths["/api/v1/public/payments/webhooks/xendit"]["post"]
