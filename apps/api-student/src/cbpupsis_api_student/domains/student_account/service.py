"""Delegate student financial reads to their shared owner."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.student_account.schemas import (
    AccountRead,
    PaymentRead,
)
from cbpupsis_shared.domains.billing import client as billing


async def read_account(db: AsyncSession, student_id: uuid.UUID) -> AccountRead:
    return await billing.read_account(db, student_id)


async def read_payments(db: AsyncSession, student_id: uuid.UUID) -> list[PaymentRead]:
    return await billing.read_payments(db, student_id)
