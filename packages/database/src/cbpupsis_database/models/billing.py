"""SQLAlchemy models for student billing accounts, fee structures, and payments."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    select,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON as JSONType

from cbpupsis_database.base import Base, UUIDMixin


class FeeStructure(UUIDMixin, Base):
    """Tuition and miscellaneous fee schedules per term and program."""

    __tablename__ = "fee_structures"

    program_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("programs.id", ondelete="CASCADE"), nullable=True, default=None
    )
    academic_term_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_terms.id", ondelete="CASCADE")
    )
    tuition_per_unit: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=2), default=Decimal("0.0"), server_default="0"
    )
    registration_fee: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=2), default=Decimal("0.0"), server_default="0"
    )
    lab_fee: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=2), default=Decimal("0.0"), server_default="0"
    )
    misc_fee: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=2), default=Decimal("0.0"), server_default="0"
    )
    late_fee_rate: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=2), default=Decimal("0.0"), server_default="0"
    )
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True, default=None)
    subsidy_covered_fees: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, default=list
    )
    installments: Mapped[list[dict] | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    __table_args__ = (
        UniqueConstraint(
            "program_id",
            "academic_term_id",
            name="uq_fee_structures_prog_term",
        ),
    )


class StudentHold(UUIDMixin, Base):
    """An unresolved or resolved hold placed on a student account."""

    __tablename__ = "student_holds"

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    hold_type: Mapped[str] = mapped_column(String(30))
    office: Mapped[str] = mapped_column(String(150))
    description: Mapped[str] = mapped_column(Text)
    amount_outstanding: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=10, scale=2), default=None
    )
    currency: Mapped[str] = mapped_column(
        String(3), default="PHP", server_default="PHP"
    )
    blocks_enrollment: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("idx_student_holds_student_unresolved", "student_id", "resolved_at"),
    )


class InstallmentPlan(UUIDMixin, Base):
    """Installment payment option configuration."""

    __tablename__ = "installment_plans"

    name: Mapped[str] = mapped_column(String(100))
    installments_count: Mapped[int] = mapped_column(SmallInteger)
    interest_rate: Mapped[Decimal] = mapped_column(
        Numeric(precision=5, scale=2), default=Decimal("0.0"), server_default="0"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )


class StudentBillingAccount(UUIDMixin, Base):
    """Term billing summary, FHE subsidy deductions, and net balance."""

    __tablename__ = "student_billing_accounts"

    assessment_snapshot: Mapped[dict | None] = mapped_column(JSON, default=None)
    overdue_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    late_penalty_amount: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=2), default=Decimal("0.00"), server_default="0"
    )
    late_penalty_applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    academic_term_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("academic_terms.id", ondelete="CASCADE")
    )
    selected_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("installment_plans.id", ondelete="SET NULL"), default=None
    )
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(precision=10, scale=2))
    fhe_subsidy_amount: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=2), default=Decimal("0.0"), server_default="0"
    )
    net_amount_due: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=10, scale=2),
        Computed(
            "gross_amount + late_penalty_amount - fhe_subsidy_amount", persisted=True
        ),
    )
    amount_paid: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=2), default=Decimal("0.0"), server_default="0"
    )
    balance: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=10, scale=2),
        Computed(
            "gross_amount + late_penalty_amount - fhe_subsidy_amount - amount_paid",
            persisted=True,
        ),
    )
    payment_status: Mapped[str] = mapped_column(
        String(30), default="unpaid", server_default="unpaid"
    )
    due_date: Mapped[date] = mapped_column(Date)

    __table_args__ = (
        CheckConstraint(
            "(late_penalty_applied_at IS NULL AND late_penalty_amount = 0) OR "
            "(late_penalty_applied_at IS NOT NULL AND late_penalty_amount > 0)",
            name="ck_billing_late_penalty_state",
        ),
        UniqueConstraint(
            "student_id",
            "academic_term_id",
            name="uq_student_billing_student_term",
        ),
    )


class BillingInstallment(UUIDMixin, Base):
    """Specific installment installment breakdown and due date."""

    __tablename__ = "billing_installments"

    billing_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student_billing_accounts.id", ondelete="CASCADE")
    )
    installment_number: Mapped[int] = mapped_column(SmallInteger)
    amount_due: Mapped[Decimal] = mapped_column(Numeric(precision=10, scale=2))
    due_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(
        String(30), default="upcoming", server_default="upcoming"
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    __table_args__ = (
        UniqueConstraint(
            "billing_account_id",
            "installment_number",
            name="uq_billing_installments_acct_num",
        ),
    )


class Payment(UUIDMixin, Base):
    """Payment transaction record with payment gateway reference."""

    __tablename__ = "payments"

    billing_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("student_billing_accounts.id", ondelete="CASCADE")
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    idempotency_key: Mapped[str] = mapped_column(String(255))
    idempotency_status: Mapped[str] = mapped_column(
        String(20), default="PROCESSING", server_default="PROCESSING"
    )
    merchant_reference: Mapped[str] = mapped_column(String(64), unique=True)
    gateway_session_id: Mapped[str | None] = mapped_column(
        String(150), unique=True, default=None
    )
    gateway_reference: Mapped[str | None] = mapped_column(
        String(150), unique=True, default=None
    )
    gateway_checkout_url: Mapped[str | None] = mapped_column(Text, default=None)
    amount: Mapped[Decimal] = mapped_column(Numeric(precision=10, scale=2))
    payment_method: Mapped[str] = mapped_column(
        String(50), default="xendit", server_default="xendit"
    )
    payment_status: Mapped[str] = mapped_column(
        String(30), default="PENDING", server_default="PENDING"
    )
    response_status: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    response_body: Mapped[dict | None] = mapped_column(
        postgresql.JSONB().with_variant(JSONType(), "sqlite"), default=None
    )
    initiated_ip: Mapped[str | None] = mapped_column(
        postgresql.INET().with_variant(String(45), "sqlite"), default=None
    )
    receipt_number: Mapped[str | None] = mapped_column(
        String(50), unique=True, default=None
    )
    failure_reason: Mapped[str | None] = mapped_column(String(255), default=None)
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("idx_fk_payments_billing_account_id", "billing_account_id"),
        Index("idx_payments_pending_created", "payment_status", "created_at"),
        UniqueConstraint(
            "student_id", "idempotency_key", name="uq_payments_student_idempotency"
        ),
    )


class PaymentWebhookEvent(UUIDMixin, Base):
    """A Xendit delivery receipt used to make webhook handling idempotent."""

    __tablename__ = "payment_webhook_events"

    webhook_id: Mapped[str] = mapped_column(String(150), unique=True)
    payment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payments.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(80))
    event_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PaymentReconciliationRun(UUIDMixin, Base):
    """One idempotent reconciliation invocation, identified by its run key."""

    __tablename__ = "payment_reconciliation_runs"

    run_key: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(
        String(20), default="RUNNING", server_default="RUNNING"
    )
    processed_count: Mapped[int] = mapped_column(default=0, server_default="0")
    settled_count: Mapped[int] = mapped_column(default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(default=0, server_default="0")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )


@event.listens_for(Payment, "before_insert")
def _fill_non_gateway_payment(mapper, connection, target: Payment) -> None:
    """Complete a payment written outside the Xendit flow (seeds, history).

    Mirrors c193's backfill of pre-gateway rows: the owner comes from the
    billing account, and both keys are ``legacy-<id>``. A gateway payment sets
    all three itself, so this leaves it untouched.
    """
    if target.student_id is None:
        target.student_id = connection.scalar(
            select(StudentBillingAccount.student_id).where(
                StudentBillingAccount.id == target.billing_account_id
            )
        )
    if target.idempotency_key is None or target.merchant_reference is None:
        if target.id is None:
            target.id = uuid.uuid4()
        legacy = f"legacy-{target.id}"
        if target.idempotency_key is None:
            target.idempotency_key = legacy
            if target.idempotency_status is None:
                target.idempotency_status = "COMPLETED"
        if target.merchant_reference is None:
            target.merchant_reference = legacy
