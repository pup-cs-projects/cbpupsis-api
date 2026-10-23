"""Schemas for the billing domain."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, computed_field


class HoldRead(BaseModel):
    type: str
    office: str
    description: str
    amount_outstanding: str = Field(
        ...,
        validation_alias=AliasChoices("amountOutstanding", "amount_outstanding"),
    )
    currency: str = "PHP"
    is_blocking: bool = Field(
        True,
        validation_alias=AliasChoices("isBlocking", "is_blocking"),
    )
    blocks: list[str] = Field(default_factory=lambda: ["ENROLLMENT", "COR"])

    @computed_field
    @property
    def amountOutstanding(self) -> str:
        return self.amount_outstanding

    @computed_field
    @property
    def amount(self) -> str:
        return self.amount_outstanding

    @computed_field
    @property
    def isBlocking(self) -> bool:
        return self.is_blocking

    @computed_field
    @property
    def blocks_enrollment(self) -> bool:
        return self.is_blocking

    @computed_field
    @property
    def blocksEnrollment(self) -> bool:
        return self.is_blocking

    model_config = ConfigDict(populate_by_name=True)


class StudentBillingAccountRead(BaseModel):
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

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
