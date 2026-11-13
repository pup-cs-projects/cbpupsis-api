"""Stable payment failures exposed by the student API."""

from __future__ import annotations

from cbpupsis_core.exceptions import AppError, ConflictError, ValidationError


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
