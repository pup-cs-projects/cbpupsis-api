"""Error hierarchy for the billing domain."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from cbpupsis_core.exceptions import ConflictError, ValidationError


class FeeValidationError(ValidationError):
    """A fee or rate value fails business validation (HTTP 422)."""

    code = "VALIDATION_ERROR"

    def __init__(self, field: str, detail: str | None = None) -> None:
        self.field = field
        msg = detail or f"Rate or fee '{field}' must not be negative."
        super().__init__(
            msg,
            code=self.code,
            response_fields={"field": field, "details": {"field": field}},
        )


class InstallmentSumInvalidError(ValidationError):
    """Installment percentage shares do not sum to 100 (HTTP 422)."""

    code = "INSTALLMENT_SUM_INVALID"

    def __init__(self, total: Decimal | str) -> None:
        super().__init__(
            f"Installment percentages must sum to exactly 100 (received {total}).",
            code=self.code,
            response_fields={"sum": str(total), "details": {"sum": str(total)}},
        )


class InstallmentDateOrderError(ValidationError):
    """An installment due date is not strictly chronological (HTTP 422)."""

    code = "INSTALLMENT_DATE_ORDER"

    def __init__(self, previous_date: date, current_date: date) -> None:
        d1 = previous_date.isoformat()
        d2 = current_date.isoformat()
        super().__init__(
            f"Installment due date {d2} is earlier than preceding due date {d1}.",
            code=self.code,
            response_fields={
                "previous_date": d1,
                "current_date": d2,
                "dates": [d1, d2],
                "details": {"previousDate": d1, "currentDate": d2, "dates": [d1, d2]},
            },
        )


class FeeStructureInUseError(ConflictError):
    """The term fee structure already has student assessments attached (HTTP 409)."""

    code = "FEE_STRUCTURE_IN_USE"

    def __init__(self) -> None:
        super().__init__(
            "The fee structure cannot be modified because student assessments "
            "have already been generated for this term.",
            code=self.code,
        )
