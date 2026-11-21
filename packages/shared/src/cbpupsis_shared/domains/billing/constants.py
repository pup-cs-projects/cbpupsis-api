"""Permissions, limits, and stable state vocabulary owned by billing."""

from decimal import Decimal

READ_ASSESSMENT = "ReadAssessment"
CREATE_PAYMENT = "CreatePayment"

PAYMENT_PENDING = "PENDING"
PAYMENT_PAID = "PAID"
PAYMENT_FAILED = "FAILED"

IDEMPOTENCY_PROCESSING = "PROCESSING"
IDEMPOTENCY_COMPLETED = "COMPLETED"
READ_PAYMENT = "ReadPayment"

# PM-approved project policy, not a reinterpretation of fee_structures.late_fee_rate.
LATE_PAYMENT_FINE = Decimal("100.00")
LATE_PAYMENT_RULE = "FIXED_100_ONCE_PER_TERM_V1"
MAX_ACCOUNT_AMOUNT = Decimal("99999999.99")

LINE_DESCRIPTIONS = {
    "TUITION": "Tuition",
    "REGISTRATION": "Registration fee",
    "LAB": "Laboratory fee",
    "MISC": "Miscellaneous fee",
}
