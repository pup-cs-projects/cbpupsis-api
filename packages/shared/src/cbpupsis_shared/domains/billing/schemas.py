"""Pydantic schemas (DTOs) for the billing domain."""

from __future__ import annotations

import uuid
from datetime import date, datetime, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class HoldRead(BaseModel):
    """A student administrative or financial hold."""

    model_config = ConfigDict(from_attributes=True)

    type: str
    office: str
    description: str
    amount_outstanding: str
    currency: str = "PHP"
    is_blocking: bool = True
    blocks_enrollment: bool = True
    blocks: list[str] = Field(default_factory=lambda: ["ENROLLMENT", "COR"])


class StudentBillingAccountRead(BaseModel):
    """A student billing summary record."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    student_id: uuid.UUID
    academic_term_id: uuid.UUID
    gross_amount: Decimal
    fhe_subsidy_amount: Decimal
    net_amount_due: Decimal | None = None
    amount_paid: Decimal
    balance: Decimal | None = None
    payment_status: str
    due_date: date
    late_penalty_amount: Decimal = Decimal("0.00")
    late_penalty_applied_at: datetime | None = None


class AssessmentCourse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_id: uuid.UUID
    units: Decimal = Field(ge=0, le=99, allow_inf_nan=False)


class AssessmentInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    student_id: uuid.UUID
    term_id: uuid.UUID
    program_id: uuid.UUID
    fee_structure_id: uuid.UUID
    courses: list[AssessmentCourse] = Field(min_length=1)
    tuition_per_unit: Decimal = Field(ge=0, allow_inf_nan=False)
    registration_fee: Decimal = Field(ge=0, allow_inf_nan=False)
    lab_fee: Decimal = Field(ge=0, allow_inf_nan=False)
    misc_fee: Decimal = Field(ge=0, allow_inf_nan=False)
    fhe_qualified: bool
    fhe_forfeited: bool
    fhe_forfeiture_reason: str | None
    subsidy_covered_fees: list[str] | None = None


class AssessmentLine(BaseModel):
    code: str
    amount: str = Field(pattern=r"^\d+\.\d{2}$")


class AssessmentRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lines: list[AssessmentLine]
    amountAssessed: str
    subsidyApplied: str
    netPayable: str
    fheQualified: bool
    fheForfeited: bool
    fheForfeitureReason: str | None


class StoredAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    inputs: AssessmentInputs
    assessment: AssessmentRead


Money = Annotated[str, Field(pattern=r"^\d+\.\d{2}$")]


class AccountLineRead(BaseModel):
    description: str
    amount: Money
    coveredByFhe: bool


class AccountRead(BaseModel):
    amountAssessed: Money
    subsidyApplied: Money
    netPayable: Money
    paymentStatus: Literal["PAID", "PARTIAL", "DUE", "OVERDUE"]
    holds: list[HoldRead]
    lines: list[AccountLineRead]
    fheQualified: bool
    fheForfeited: bool
    fheForfeitureReason: str | None


class PaymentRead(BaseModel):
    date: datetime
    amount: Money
    reference: str
    status: str


class ProgramTuitionRate(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    program_id: uuid.UUID = Field(
        validation_alias=AliasChoices("programId", "program_id")
    )
    tuition_per_unit: Decimal = Field(
        validation_alias=AliasChoices("tuitionPerUnit", "tuition_per_unit")
    )


class ProgramTuitionRateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    program_id: uuid.UUID = Field(serialization_alias="programId")
    tuition_per_unit: str = Field(serialization_alias="tuitionPerUnit")


class InstallmentItem(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    installment_number: int = Field(
        ge=1, validation_alias=AliasChoices("installmentNumber", "installment_number")
    )
    percentage: Decimal = Field(validation_alias=AliasChoices("percentage", "percent"))
    due_date: date = Field(validation_alias=AliasChoices("dueDate", "due_date"))


class InstallmentItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    installment_number: int = Field(serialization_alias="installmentNumber")
    percentage: str
    due_date: date = Field(serialization_alias="dueDate")


class FeeStructureSaveRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    tuition_per_unit: Decimal = Field(
        default=Decimal("0.00"),
        validation_alias=AliasChoices("tuitionPerUnit", "tuition_per_unit"),
    )
    program_tuition_rates: list[ProgramTuitionRate] = Field(
        default_factory=list,
        validation_alias=AliasChoices(
            "programTuitionRates", "program_tuition_rates", "programs"
        ),
    )
    registration_fee: Decimal = Field(
        default=Decimal("0.00"),
        validation_alias=AliasChoices("registrationFee", "registration_fee"),
    )
    lab_fee: Decimal = Field(
        default=Decimal("0.00"),
        validation_alias=AliasChoices("labFee", "lab_fee"),
    )
    misc_fee: Decimal = Field(
        default=Decimal("0.00"),
        validation_alias=AliasChoices("miscFee", "misc_fee"),
    )
    late_fee_rate: Decimal = Field(
        default=Decimal("0.00"),
        validation_alias=AliasChoices("lateFeeRate", "late_fee_rate"),
    )
    due_date: date | None = Field(
        default=None,
        validation_alias=AliasChoices("dueDate", "due_date"),
    )
    subsidy_covered_fees: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("subsidyCoveredFees", "subsidy_covered_fees"),
    )
    installments: list[InstallmentItem] = Field(
        default_factory=list,
        validation_alias=AliasChoices("installments"),
    )


class FeeStructureRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    academic_term_id: uuid.UUID = Field(serialization_alias="academicTermId")
    tuition_per_unit: str = Field(serialization_alias="tuitionPerUnit")
    program_tuition_rates: list[ProgramTuitionRateRead] = Field(
        default_factory=list,
        serialization_alias="programTuitionRates",
    )
    registration_fee: str = Field(serialization_alias="registrationFee")
    lab_fee: str = Field(serialization_alias="labFee")
    misc_fee: str = Field(serialization_alias="miscFee")
    late_fee_rate: str = Field(serialization_alias="lateFeeRate")
    due_date: date | None = Field(
        default=None,
        serialization_alias="dueDate",
    )
    subsidy_covered_fees: list[str] = Field(
        default_factory=list,
        serialization_alias="subsidyCoveredFees",
    )
    installments: list[InstallmentItemRead] = Field(
        default_factory=list,
        serialization_alias="installments",
    )


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2, allow_inf_nan=False)


class PaymentCreated(BaseModel):
    paymentId: uuid.UUID
    state: str
    checkoutUrl: str


class PaymentRead(BaseModel):
    paymentId: uuid.UUID
    state: str
    amount: str
    currency: str = "PHP"
    gatewayReference: str | None = None
    receiptNumber: str | None = None
    paidAt: datetime | None = None
    createdAt: datetime


class XenditWebhookData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reference_id: str
    payment_session_id: str | None = None
    payment_id: str | None = None
    status: str | None = None


class XenditWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event: str
    created: datetime
    data: XenditWebhookData


class ReconciliationResult(BaseModel):
    runKey: str
    processed: int
    settled: int
    failed: int
