"""Regressions for native PostgreSQL INET values in sign-in ledger responses."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from ipaddress import IPv4Address, IPv6Address
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.audit import AuthAuditLog
from cbpupsis_shared.domains.audit import repository
from cbpupsis_shared.domains.audit.schemas import SignInAttemptRead

ADDRESSES = [
    pytest.param("127.0.0.1", id="ipv4-string"),
    pytest.param("2001:db8::1", id="ipv6-string"),
    pytest.param(IPv4Address("127.0.0.1"), id="ipv4-native"),
    pytest.param(IPv6Address("2001:db8::1"), id="ipv6-native"),
    pytest.param(None, id="unknown-address"),
]


@pytest.fixture
def attempt() -> AuthAuditLog:
    return AuthAuditLog(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        attempted_id="audit-test@example.com",
        ip_address=None,
        user_agent="audit-inet-regression",
        success=False,
        failure_reason="invalid_credentials",
        is_suspicious=False,
        mfa_used=False,
        created_at=datetime(2026, 10, 19, tzinfo=UTC),
    )


@pytest.mark.parametrize("address", ADDRESSES)
def test_native_and_string_addresses_serialize_as_string_or_null(
    attempt: AuthAuditLog, address: object
) -> None:
    """AC-001.6: ORM-loaded IPs remain readable without changing JSON types."""
    attempt.ip_address = address
    response = SignInAttemptRead.model_validate(attempt)
    body = response.model_dump(mode="json")

    assert body["ip_address"] == (None if address is None else str(address))
    assert body["created_at"] == "2026-10-19T08:00:00+08:00"
    assert body["user_id"] == str(attempt.user_id)
    assert body["success"] is False
    assert body["failure_reason"] == "invalid_credentials"


@pytest.mark.parametrize("address", ["not-an-ip", object()])
def test_invalid_addresses_are_rejected(attempt: AuthAuditLog, address: object) -> None:
    attempt.ip_address = address
    with pytest.raises(ValidationError) as caught:
        SignInAttemptRead.model_validate(attempt)
    assert caught.value.errors()[0]["loc"] == ("ip_address",)


def test_ip_address_contract_is_a_nullable_ip_string() -> None:
    field = SignInAttemptRead.model_json_schema()["properties"]["ip_address"]
    assert field["anyOf"] == [
        {"type": "string", "format": "ipvanyaddress"},
        {"type": "null"},
    ]


@pytest.mark.parametrize("address", ADDRESSES)
async def test_superadmin_can_read_sign_in_attempts_with_native_addresses(
    client: AsyncClient,
    db: AsyncSession,
    superadmin_headers: dict[str, str],
    registered_user: dict[str, str],
    grant,
    monkeypatch: pytest.MonkeyPatch,
    attempt: AuthAuditLog,
    address: object,
) -> None:
    """AC-001.6: a valid ledger row must not turn its protected GET into 500.

    SQLite returns strings, unlike PostgreSQL/asyncpg. Substitute only the
    repository result so authorization and response validation remain real.
    """
    await grant(uuid.UUID(registered_user["id"]), "ReadAllAuditEntry")
    attempt.ip_address = address
    read = AsyncMock(return_value=([attempt], 1))
    monkeypatch.setattr(repository, "list_sign_in_attempts", read)

    response = await client.get(
        "/api/v1/audit/sign-in-attempts",
        headers=superadmin_headers,
        params={"limit": 1, "offset": 0},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["limit"] == 1
    assert body["offset"] == 0
    assert len(body["items"]) == 1
    row = body["items"][0]
    assert row["id"] == str(attempt.id)
    assert row["user_id"] == str(attempt.user_id)
    assert row["ip_address"] == (None if address is None else str(address))
    assert row["created_at"] == "2026-10-19T08:00:00+08:00"
    assert row["user_agent"] == "audit-inet-regression"
    assert row["success"] is False
    assert row["failure_reason"] == "invalid_credentials"
    read.assert_awaited_once_with(db, limit=1, offset=0)


async def test_non_superadmin_cannot_read_sign_in_attempts_even_with_permission(
    client: AsyncClient,
    auth_headers: dict[str, str],
    registered_user: dict[str, str],
    grant,
) -> None:
    await grant(uuid.UUID(registered_user["id"]), "ReadAllAuditEntry")
    response = await client.get("/api/v1/audit/sign-in-attempts", headers=auth_headers)
    assert response.status_code == 403, response.text


async def test_sign_in_attempts_require_authentication(client: AsyncClient) -> None:
    response = await client.get("/api/v1/audit/sign-in-attempts")
    assert response.status_code == 401, response.text
