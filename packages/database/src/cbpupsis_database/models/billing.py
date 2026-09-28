"""SQLAlchemy models for student billing accounts, fee structures, and payments."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from cbpupsis_database.base import Base, UUIDMixin


class FeeStructure(UUIDMixin, Base):
    """Tuition and miscellaneous fee schedules per term and program."""

    __tablename__ = "fee_structures"

    program_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("programs.id", ondelete="CASCADE")
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

    __table_args__ = (
        UniqueConstraint(
            "program_id",
            "academic_term_id",
            name="uq_fee_structures_prog_term",
        ),
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
        Computed("gross_amount - fhe_subsidy_amount", persisted=True),
    )
    amount_paid: Mapped[Decimal] = mapped_column(
        Numeric(precision=10, scale=2), default=Decimal("0.0"), server_default="0"
    )
    balance: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=10, scale=2),
        Computed("gross_amount - fhe_subsidy_amount - amount_paid", persisted=True),
    )
    payment_status: Mapped[str] = mapped_column(
        String(30), default="unpaid", server_default="unpaid"
    )
    due_date: Mapped[date] = mapped_column(Date)

    __table_args__ = (
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
    gateway_reference: Mapped[str] = mapped_column(String(150), unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(precision=10, scale=2))
    payment_method: Mapped[str] = mapped_column(String(50))
    payment_status: Mapped[str] = mapped_column(String(30))
    receipt_number: Mapped[str | None] = mapped_column(
        String(50), unique=True, default=None
    )
    failure_reason: Mapped[str | None] = mapped_column(String(255), default=None)
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )

    __table_args__ = (
        Index("idx_fk_payments_billing_account_id", "billing_account_id"),
    )
