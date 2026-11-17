"""Caller-owned, row-locked overdue processing under the approved fixed rule."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from cbpupsis_core.events import Event
from cbpupsis_shared.domains.audit import client as audit
from cbpupsis_shared.domains.billing.constants import (
    LATE_PAYMENT_FINE,
    LATE_PAYMENT_RULE,
    MAX_ACCOUNT_AMOUNT,
)
from cbpupsis_shared.domains.billing.exceptions import BillingAccountUnavailableError
from cbpupsis_shared.outbox import publish_transactional

MANILA = timezone(timedelta(hours=8))


def account_balance(row) -> Decimal:
    return max(
        row.gross_amount
        + row.late_penalty_amount
        - row.fhe_subsidy_amount
        - row.amount_paid,
        Decimal(0),
    )


async def stage_overdue(db, row, *, as_of: datetime | None = None) -> None:
    """Caller owns the account lock and commit; no independent transaction here."""
    now = as_of or datetime.now(UTC)
    if now.astimezone(MANILA).date() <= row.due_date:
        return
    principal_due = row.gross_amount - row.fhe_subsidy_amount - row.amount_paid
    if principal_due > 0 and row.late_penalty_applied_at is None:
        net_due = row.gross_amount - row.fhe_subsidy_amount + LATE_PAYMENT_FINE
        if net_due > MAX_ACCOUNT_AMOUNT:
            raise BillingAccountUnavailableError
        row.late_penalty_amount = LATE_PAYMENT_FINE
        row.late_penalty_applied_at = now
        audit.stage(
            db,
            action="billing.late_penalty_applied",
            actor_id=None,
            target_type="student_billing_account",
            target_id=row.id,
            prior_state={"latePenaltyAmount": "0.00"},
            new_state={"latePenaltyAmount": f"{LATE_PAYMENT_FINE:.2f}"},
            payload={
                "rule": LATE_PAYMENT_RULE,
                "studentId": str(row.student_id),
                "termId": str(row.academic_term_id),
                "dueDate": row.due_date.isoformat(),
                "timeUtc8": now.astimezone(MANILA).isoformat(),
            },
            occurred_at=now,
        )
    balance = account_balance(row)
    if balance > 0 and row.overdue_notified_at is None:
        row.overdue_notified_at = now
        publish_transactional(
            db,
            Event(
                name="billing.account_overdue",
                payload={
                    "user_id": str(row.student_id),
                    "billing_account_id": str(row.id),
                    "term_id": str(row.academic_term_id),
                    "amount": f"{balance:.2f}",
                    "due_date": row.due_date.isoformat(),
                    "penalty_amount": f"{row.late_penalty_amount:.2f}",
                    "rule": LATE_PAYMENT_RULE,
                },
            ),
        )
    await db.flush()
