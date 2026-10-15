"""Password recovery contracts, transactional failure paths, and durable delivery."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from passlib.context import CryptContext
from sqlalchemy import func, select, update

from cbpupsis_core.config import Settings, settings
from cbpupsis_core.emails import ConsoleEmailSender
from cbpupsis_core.events import Event
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.auth import (
    AuthenticationFailure,
    AuthenticationLockout,
    AuthFailureStep,
    OneTimeToken,
    RefreshToken,
    TokenPurpose,
)
from cbpupsis_database.models.outbox import OutboxMessage
from cbpupsis_database.models.users import User
from cbpupsis_shared import worker
from cbpupsis_shared.domains.audit.schemas import AuditEntryRead
from cbpupsis_shared.domains.auth import delivery, service
from cbpupsis_shared.domains.auth.security import verify_password
from cbpupsis_shared.domains.auth.subscribers import register_auth_subscribers
from tests.conftest import token_from_email


async def request_link(client, registered_user, sent_emails, drain_outbox):
    sent_emails.clear()
    result = await client.post(
        "/api/v1/auth/forgot-password", json={"email": registered_user["email"]}
    )
    assert result.status_code == 202
    assert sent_emails == []
    await drain_outbox()
    assert len(sent_emails) == 1
    return token_from_email(sent_emails[0])


async def test_request_queues_encrypted_delivery_and_uniform_audit(
    client, db, registered_user, sent_emails, drain_outbox
):
    sent_emails.clear()
    known = await client.post(
        "/api/v1/auth/forgot-password", json={"email": registered_user["email"]}
    )
    unknown = await client.post(
        "/api/v1/auth/forgot-password", json={"email": "unknown@example.com"}
    )
    assert known.status_code == unknown.status_code == 202
    assert known.content == unknown.content
    assert sent_emails == []
    messages = list((await db.scalars(select(OutboxMessage))).all())
    assert len(messages) == 1
    assert messages[0].event_name == "auth.password_reset_email"
    await drain_outbox()
    raw = token_from_email(sent_emails[0])
    assert raw not in json.dumps(messages[0].payload)
    records = list(
        (
            await db.scalars(
                select(AuditEntry).where(
                    AuditEntry.action == "auth.password_reset_requested"
                )
            )
        ).all()
    )
    assert len(records) == 2
    assert {r.payload["actor"] for r in records} == {registered_user["id"], "anonymous"}
    for entry in records:
        assert entry.payload["ip_address"] == "127.0.0.1"
        assert entry.payload["outcome"] == "accepted"
        assert (
            AuditEntryRead.model_validate(entry)
            .model_dump(mode="json")["occurred_at"]
            .endswith("+08:00")
        )
        assert raw not in json.dumps(entry.payload)


@pytest.mark.parametrize("invalid", ["abc", "missing-at-sign", "", None, 123, [], {}])
async def test_invalid_email_has_required_error(client, invalid):
    result = await client.post("/api/v1/auth/forgot-password", json={"email": invalid})
    assert result.status_code == 400
    assert result.json()["code"] == "INVALID_EMAIL_ADDRESS"


async def test_eight_character_password_works_and_abc_lists_all_unmet_rules(
    client, db, registered_user, sent_emails, drain_outbox
):
    token = await request_link(client, registered_user, sent_emails, drain_outbox)
    weak = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": "abc"}
    )
    assert weak.status_code == 422
    assert weak.json()["code"] == "AUTH_PASSWORD_TOO_WEAK"
    assert set(weak.json()["unmet_rules"]) == {
        "at least 8 characters",
        "uppercase",
        "digit",
        "symbol",
    }
    success = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": "Strong1!"}
    )
    assert success.status_code == 200
    user = await db.get(User, uuid.UUID(registered_user["id"]))
    assert user.password_hash.startswith("$2b$")
    assert int(user.password_hash.split("$")[2]) >= 12
    assert verify_password("Strong1!", user.password_hash)


@pytest.mark.parametrize("password", ["Abcdef12 ", "A1!" + "界" * 30])
async def test_whitespace_is_not_a_symbol_and_bcrypt_inputs_are_not_truncated(
    client, registered_user, sent_emails, drain_outbox, password
):
    token = await request_link(client, registered_user, sent_emails, drain_outbox)
    result = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": password}
    )
    assert result.status_code == 422
    assert result.json()["code"] == "AUTH_PASSWORD_TOO_WEAK"


async def test_link_can_be_checked_without_consumption_and_defaults_to_24_hours(
    client, db, registered_user, sent_emails, drain_outbox
):
    token = await request_link(client, registered_user, sent_emails, drain_outbox)
    record = await db.scalar(
        select(OneTimeToken).where(OneTimeToken.purpose == TokenPurpose.password_reset)
    )
    expires = record.expires_at.replace(tzinfo=UTC)
    assert (
        timedelta(hours=23, minutes=59)
        < expires - datetime.now(UTC)
        <= timedelta(hours=24)
    )
    for _ in range(2):
        check = await client.post(
            "/api/v1/auth/reset-password", json={"token": token, "verify_only": True}
        )
        assert check.status_code == 200
        assert check.json()["valid"] is True
        assert check.json()["minimumLength"] == 8
    await db.refresh(record)
    assert record.consumed_at is None
    await db.execute(
        update(OneTimeToken)
        .where(OneTimeToken.id == record.id)
        .values(expires_at=datetime.now(UTC) - timedelta(hours=1))
    )
    await db.commit()
    expired = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "verify_only": True}
    )
    assert expired.status_code == 410
    assert expired.json()["code"] == "AUTH_RESET_TOKEN_EXPIRED"
    assert "request a new" in expired.json()["detail"]


async def test_reset_ends_both_access_and_refresh_sessions_and_clears_lockout(
    client, db, registered_user, sent_emails, drain_outbox
):
    uid = uuid.UUID(registered_user["id"])
    pairs = []
    for _ in range(2):
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        pairs.append(login.json())
    db.add(
        AuthenticationLockout(
            user_id=uid, locked_until=datetime.now(UTC) + timedelta(minutes=15)
        )
    )
    db.add(
        AuthenticationFailure(
            user_id=uid, step=AuthFailureStep.credentials, occurred_at=datetime.now(UTC)
        )
    )
    await db.commit()
    token = await request_link(client, registered_user, sent_emails, drain_outbox)
    reset = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": "Strong1!"}
    )
    assert reset.status_code == 200
    assert reset.json()["sessionsRevoked"] == 2
    for pair in pairs:
        access = await client.get(
            "/api/v1/auth/whoami",
            headers={"Authorization": "Bearer " + pair["access_token"]},
        )
        refresh = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
        )
        assert access.status_code == refresh.status_code == 401
        assert access.json()["code"] == refresh.json()["code"] == "AUTH_SESSION_EXPIRED"
    assert await db.get(AuthenticationLockout, uid) is None
    assert (
        await db.scalar(
            select(func.count())
            .select_from(AuthenticationFailure)
            .where(AuthenticationFailure.user_id == uid)
        )
        == 0
    )
    new_login = await client.post(
        "/api/v1/auth/login",
        json={"email": registered_user["email"], "password": "Strong1!"},
    )
    assert new_login.status_code == 200
    assert (
        await client.get(
            "/api/v1/auth/whoami",
            headers={"Authorization": "Bearer " + new_login.json()["access_token"]},
        )
    ).status_code == 200


async def test_failure_rolls_back_password_consumption_revocation_and_notice(
    client, db, registered_user, sent_emails, drain_outbox, monkeypatch
):
    uid = uuid.UUID(registered_user["id"])
    token = await request_link(client, registered_user, sent_emails, drain_outbox)
    login = await client.post(
        "/api/v1/auth/login",
        json={
            "email": registered_user["email"],
            "password": registered_user["password"],
        },
    )
    db.add(
        AuthenticationLockout(
            user_id=uid, locked_until=datetime.now(UTC) + timedelta(minutes=15)
        )
    )
    await db.commit()

    async def fail(*args, **kwargs):
        raise RuntimeError("injected clear-lockout failure")

    monkeypatch.setattr(service.repository, "clear_authentication_lockout", fail)
    with pytest.raises(RuntimeError):
        await service.reset_password(db, token, "Strong1!")
    user = await db.get(User, uid)
    assert verify_password(registered_user["password"], user.password_hash)
    assert user.session_version == 0
    record = await db.scalar(
        select(OneTimeToken).where(OneTimeToken.purpose == TokenPurpose.password_reset)
    )
    assert record.consumed_at is None
    assert await db.get(AuthenticationLockout, uid) is not None
    assert (
        await client.get(
            "/api/v1/auth/whoami",
            headers={"Authorization": "Bearer " + login.json()["access_token"]},
        )
    ).status_code == 200
    assert (
        await db.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .where(OutboxMessage.event_name == "auth.password_changed")
        )
        == 0
    )
    audits = list(
        (
            await db.scalars(
                select(AuditEntry).where(
                    AuditEntry.action == "auth.password_reset_completed"
                )
            )
        ).all()
    )
    assert len(audits) == 1
    assert audits[0].payload["outcome"] == "failed"


async def test_delivery_retries_same_token_with_backoff_and_receipt_deduplication(
    client, db, registered_user, sent_emails, drain_outbox, monkeypatch
):
    sent_emails.clear()
    await client.post(
        "/api/v1/auth/forgot-password", json={"email": registered_user["email"]}
    )
    message = await db.scalar(
        select(OutboxMessage).where(
            OutboxMessage.event_name == "auth.password_reset_email"
        )
    )
    original_payload = dict(message.payload)
    original_send = delivery.send_email

    async def fail(**kwargs):
        return False

    monkeypatch.setattr(delivery, "send_email", fail)
    await drain_outbox()
    await db.refresh(message)
    assert message.status == "pending"
    assert message.attempts == 1
    assert message.available_at.replace(tzinfo=UTC) > datetime.now(UTC)
    assert sent_emails == []
    assert message.payload == original_payload
    monkeypatch.setattr(delivery, "send_email", original_send)
    message.available_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    await drain_outbox()
    assert len(sent_emails) == 1
    await delivery.deliver_reset_email_on(
        db,
        Event(name=message.event_name, payload=message.payload),
        message_id=message.id,
    )
    assert len(sent_emails) == 1
    assert (
        await db.scalar(
            select(func.count())
            .select_from(OneTimeToken)
            .where(OneTimeToken.purpose == TokenPurpose.password_reset)
        )
        == 1
    )


async def test_address_limit_is_uniform_across_ips_and_keeps_existing_link_usable(
    client, db, registered_user, sent_emails, drain_outbox, monkeypatch
):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "password_reset_request_limit", 2)
    tokens = []
    for i in range(2):
        sent_emails.clear()
        result = await client.post(
            "/api/v1/auth/forgot-password",
            json={"email": registered_user["email"]},
            headers={"X-Forwarded-For": f"203.0.113.{i + 1}"},
        )
        assert result.status_code == 202
        await drain_outbox()
        tokens.append(token_from_email(sent_emails[0]))
    refused = await client.post(
        "/api/v1/auth/forgot-password",
        json={"email": registered_user["email"].upper()},
        headers={"X-Forwarded-For": "203.0.113.3"},
    )
    assert refused.status_code == 429
    assert refused.json()["code"] == "RATE_LIMIT_EXCEEDED"
    assert int(refused.headers["Retry-After"]) > 0
    other = await client.post(
        "/api/v1/auth/forgot-password", json={"email": "someone-else@example.com"}
    )
    assert other.status_code == 202
    for token in tokens:
        assert (
            await client.post(
                "/api/v1/auth/reset-password",
                json={"token": token, "verify_only": True},
            )
        ).status_code == 200
    reset = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": tokens[0], "new_password": "Strong1!"},
    )
    assert reset.status_code == 200
    assert (
        await client.post(
            "/api/v1/auth/reset-password",
            json={"token": tokens[1], "verify_only": True},
        )
    ).status_code == 410
    assert (
        await db.scalar(
            select(func.count())
            .select_from(OneTimeToken)
            .where(OneTimeToken.purpose == TokenPurpose.password_reset)
        )
        == 2
    )


async def test_expired_refresh_tokens_do_not_inflate_sessions_revoked(
    client, db, registered_user, sent_emails, drain_outbox
):
    uid = uuid.UUID(registered_user["id"])
    db.add(
        RefreshToken(
            user_id=uid,
            jti=str(uuid.uuid4()),
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    await db.commit()
    token = await request_link(client, registered_user, sent_emails, drain_outbox)
    reset = await client.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": "Strong1!"}
    )
    assert reset.json()["sessionsRevoked"] == 0


async def test_console_transport_does_not_log_the_token_or_password(caplog):
    with caplog.at_level(logging.INFO):
        await ConsoleEmailSender().send(
            to="test@example.com",
            subject="Reset",
            body="token=synthetic-token-value password=synthetic-password-value",
        )
    assert "synthetic-token-value" not in caplog.text
    assert "synthetic-password-value" not in caplog.text


def test_legacy_argon2_passwords_still_verify():
    digest = CryptContext(schemes=["argon2"]).hash("legacy-password")
    assert verify_password("legacy-password", digest)
    assert not verify_password("incorrect", digest)


def test_weak_password_service_validation_and_cost_floor():
    with pytest.raises(ValueError):
        Settings(database_url="sqlite://", jwt_secret="x" * 32, bcrypt_rounds=11)


def test_worker_registers_reset_delivery_idempotently(monkeypatch):
    monkeypatch.setattr(worker, "_HANDLERS", {})
    register_auth_subscribers()
    register_auth_subscribers()
    assert worker.handlers_for("auth.password_reset_email") == (
        delivery.deliver_reset_email,
    )


async def test_expired_queue_job_is_skipped_without_mailing(
    client, db, registered_user, sent_emails, drain_outbox
):
    sent_emails.clear()
    await client.post(
        "/api/v1/auth/forgot-password", json={"email": registered_user["email"]}
    )
    await db.execute(
        update(OneTimeToken)
        .where(OneTimeToken.purpose == TokenPurpose.password_reset)
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db.commit()
    await drain_outbox()
    assert sent_emails == []
    message = await db.scalar(
        select(OutboxMessage).where(
            OutboxMessage.event_name == "auth.password_reset_email"
        )
    )
    assert message.status == "dispatched"


async def test_configured_lifetime_applies_to_new_links(
    client, db, registered_user, sent_emails, monkeypatch
):
    monkeypatch.setattr(settings, "password_reset_ttl_minutes", 60)
    await client.post(
        "/api/v1/auth/forgot-password", json={"email": registered_user["email"]}
    )
    record = await db.scalar(
        select(OneTimeToken).where(OneTimeToken.purpose == TokenPurpose.password_reset)
    )
    expiry = record.expires_at.replace(tzinfo=UTC)
    assert timedelta(minutes=59) < expiry - datetime.now(UTC) <= timedelta(minutes=60)


async def test_unknown_address_has_same_shared_allowance(client, db, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "password_reset_request_limit", 1)
    accepted = await client.post(
        "/api/v1/auth/forgot-password", json={"email": "unknown@example.com"}
    )
    refused = await client.post(
        "/api/v1/auth/forgot-password", json={"email": "unknown@example.com"}
    )
    assert accepted.status_code == 202
    assert refused.status_code == 429
    assert refused.json()["code"] == "RATE_LIMIT_EXCEEDED"
    assert await db.scalar(select(func.count()).select_from(OutboxMessage)) == 0
    assert await db.scalar(select(func.count()).select_from(AuditEntry)) == 2


async def test_request_and_completion_write_one_audit_each_without_secrets(
    client, db, registered_user, sent_emails, drain_outbox, caplog
):
    with caplog.at_level(logging.INFO):
        token = await request_link(client, registered_user, sent_emails, drain_outbox)
        password = "Audit-secret-password-1!"
        result = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": password},
        )
    assert result.status_code == 200
    rows = list((await db.scalars(select(AuditEntry))).all())
    assert len(rows) == 2
    assert {row.action for row in rows} == {
        "auth.password_reset_requested",
        "auth.password_reset_completed",
    }
    assert {row.payload["outcome"] for row in rows} == {"accepted", "success"}
    for row in rows:
        assert str(row.actor_id) == registered_user["id"]
        assert row.payload["ip_address"] == "127.0.0.1"
        serialized = json.dumps(
            AuditEntryRead.model_validate(row).model_dump(mode="json")
        )
        assert "+08:00" in serialized
        assert token not in serialized
        assert password not in serialized
    assert token not in caplog.text
    assert password not in caplog.text
