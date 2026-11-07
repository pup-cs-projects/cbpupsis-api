"""Pydantic schemas (DTOs) for the billing domain."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


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
