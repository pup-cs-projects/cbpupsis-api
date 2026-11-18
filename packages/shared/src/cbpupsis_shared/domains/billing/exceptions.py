"""Error hierarchy for the billing domain."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from cbpupsis_core.exceptions import AppError, ConflictError, ValidationError


class BillingAccountUnavailableError(AppError):
    status_code = 503
    code = "SERVICE_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__(
            "The account cannot be safely computed with the available financial data.",
            headers={"Retry-After": "30"},
        )


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


class IdempotencyKeyRequiredError(AppError):
    code = "IDEMPOTENCY_KEY_REQUIRED"

    def __init__(self) -> None:
        super().__init__("A non-empty Idempotency-Key header is required.")


class PaymentInProgressError(ConflictError):
    code = "PAYMENT_IN_PROGRESS"

    def __init__(self, retry_after: int) -> None:
        super().__init__(
            "A payment with this idempotency key is still being created.",
            headers={"Retry-After": str(retry_after)},
        )


class AmountExceedsBalanceError(ValidationError):
    code = "AMOUNT_EXCEEDS_BALANCE"

    def __init__(self) -> None:
        super().__init__("The payment amount exceeds the available balance.")


class PaymentGatewayError(AppError):
    status_code = 502
    code = "PAYMENT_GATEWAY_ERROR"

    def __init__(self, reason: str) -> None:
        super().__init__(
            "The payment gateway rejected the request.",
            response_fields={"reason": reason},
        )


class PaymentGatewayTimeoutError(AppError):
    status_code = 504
    code = "PAYMENT_GATEWAY_TIMEOUT"

    def __init__(self) -> None:
        super().__init__(
            "The payment gateway did not respond in time. The attempt remains pending."
        )


class WebhookSignatureInvalidError(AppError):
    status_code = 401
    code = "WEBHOOK_SIGNATURE_INVALID"

    def __init__(self) -> None:
        super().__init__("The webhook signature or timestamp is invalid.")


class PaymentReceiptUnavailableError(ConflictError):
    def __init__(self) -> None:
        super().__init__("A receipt is available only after payment settlement.")
