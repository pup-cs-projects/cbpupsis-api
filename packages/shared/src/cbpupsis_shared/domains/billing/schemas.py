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
