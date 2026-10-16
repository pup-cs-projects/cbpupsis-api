"""A sign-in never fails because the caller's address is unknown.

The address is recorded for the owner and auditors, never compared later, so a
request without a usable one stores NULL instead of being refused.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.audit import AuthAuditLog
from cbpupsis_database.models.auth import UserActiveSession


async def _session_addresses(db: AsyncSession, user_id: uuid.UUID) -> list:
    return list(
        (
            await db.scalars(
                select(UserActiveSession.ip_address).where(
                    UserActiveSession.user_id == user_id
                )
            )
        ).all()
    )


async def _ledger_addresses(db: AsyncSession, user_id: uuid.UUID) -> list:
    return list(
        (
            await db.scalars(
                select(AuthAuditLog.ip_address).where(AuthAuditLog.user_id == user_id)
            )
        ).all()
    )


async def test_login_without_an_address_signs_in_and_stores_null(
    addressless_client: AsyncClient,
    db: AsyncSession,
    registered_user: dict[str, str],
) -> None:
    response = await addressless_client.post(
        "/api/v1/auth/login",
        json={
            "email": registered_user["email"],
            "password": registered_user["password"],
        },
    )
    assert response.status_code == 200, response.text

    user_id = uuid.UUID(registered_user["id"])
    assert await _session_addresses(db, user_id) == [None]
    assert await _ledger_addresses(db, user_id) == [None]


async def test_change_password_keeps_a_null_address_null(
    addressless_client: AsyncClient,
    db: AsyncSession,
    registered_user: dict[str, str],
    sent_emails: list[dict[str, str]],
) -> None:
    """str(None) would be "None", which is not a valid INET value."""
    login = await addressless_client.post(
        "/api/v1/auth/login",
        json={
            "email": registered_user["email"],
            "password": registered_user["password"],
        },
    )
    changed = await addressless_client.post(
        "/api/v1/auth/change-password",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        json={
            "current_password": registered_user["password"],
            "new_password": "a-freshly-chosen-password",
        },
    )
    assert changed.status_code == 200, changed.text

    assert await _session_addresses(db, uuid.UUID(registered_user["id"])) == [None]
