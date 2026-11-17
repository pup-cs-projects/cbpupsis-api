"""Private, session-owned student account and payment history reads."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.student_account import service
from cbpupsis_api_student.domains.student_account.schemas import (
    AccountRead,
    PaymentRead,
)
from cbpupsis_database.session import get_db
from cbpupsis_shared.domains.auth.dependencies import CurrentUser, get_current_user
from cbpupsis_shared.domains.auth.exceptions import ResourceNotFoundError
from cbpupsis_shared.domains.billing.constants import READ_ASSESSMENT, READ_PAYMENT
from cbpupsis_shared.domains.iam.dependencies import require_permission


async def private_response(request: Request) -> None:
    request.state.private_response = True


async def account_student(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if user.role != "student":
        raise ResourceNotFoundError
    return user


async def assessment_reader(
    user: CurrentUser = Depends(account_student),
    _authorized: CurrentUser = Depends(require_permission(READ_ASSESSMENT)),
) -> CurrentUser:
    return user


async def payment_reader(
    user: CurrentUser = Depends(account_student),
    _authorized: CurrentUser = Depends(require_permission(READ_PAYMENT)),
) -> CurrentUser:
    return user


router = APIRouter(dependencies=[Depends(private_response)])
ERROR_RESPONSES = {
    401: {"description": "AUTH_SESSION_EXPIRED: the student session has expired."},
    403: {"description": "The student lacks the operation's permission."},
    404: {"description": "RESOURCE_NOT_FOUND: non-student session or no owned record."},
}


@router.get(
    "/account",
    response_model=AccountRead,
    summary="Read My Student Account",
    description=(
        "Read the signed-in student's current-term shared assessment, per-line "
        "FHE coverage, forfeiture decision, payment status, and holds. Requires "
        "ReadAssessment. Money is returned as decimal strings. netPayable is "
        "the remaining balance after subsidy and recorded payments, including "
        "a non-covered PHP 100 fine once per term account after the due date "
        "if principal remains unpaid, floored at zero. Non-student sessions "
        "receive 404 RESOURCE_NOT_FOUND. Responses "
        "are private and must not be cached."
    ),
    responses={
        **ERROR_RESPONSES,
        503: {
            "description": (
                "SERVICE_UNAVAILABLE: financial data cannot be verified or "
                "the computed account exceeds supported storage precision."
            )
        },
    },
)
async def read_account(
    user: CurrentUser = Depends(assessment_reader),
    db: AsyncSession = Depends(get_db),
) -> AccountRead:
    return await service.read_account(db, user.id)


@router.get(
    "/payments",
    response_model=list[PaymentRead],
    summary="Read My Payment History",
    description=(
        "Read only the signed-in student's payments across terms, newest first "
        "by payment date (creation date for unpaid transactions), then creation "
        "date and identifier for stable ties. Requires ReadPayment. Each item "
        "contains date, decimal-string amount, gateway reference, and status. "
        "An empty history is []. Non-student sessions receive 404 "
        "RESOURCE_NOT_FOUND. Responses are private and must not be cached."
    ),
    responses=ERROR_RESPONSES,
)
async def read_payments(
    user: CurrentUser = Depends(payment_reader),
    db: AsyncSession = Depends(get_db),
) -> list[PaymentRead]:
    return await service.read_payments(db, user.id)
