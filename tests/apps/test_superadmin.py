"""US-AUTH-004 acceptance checks for the fresh implementation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select

from cbpupsis_api_admin.domains.admin_auth.security import decrypt_secret, totp_at
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.auth import (
    RefreshToken,
    UserActiveSession,
    UserMfaCredential,
)
from cbpupsis_database.models.iam import Group
from cbpupsis_database.models.superadmin_ops import SystemBackup
from cbpupsis_shared.domains.auth import service as auth_service
from cbpupsis_shared.domains.auth.security import decode_token
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP, SUPERADMIN_GROUP
from cbpupsis_shared.domains.users import service as users_service


@pytest.fixture
async def superadmin_account(db, registered_user):
    group = await iam_service.create_group(db, name=SUPERADMIN_GROUP)
    await iam_service.add_user_to_group(db, uuid.UUID(registered_user["id"]), group.id)
    return registered_user


async def test_AC0041_password_is_only_a_challenge(client, superadmin_account):
    response = await client.post(
        "/api/v1/auth/superadmin/login",
        json={
            "email": superadmin_account["email"],
            "password": superadmin_account["password"],
        },
    )
    assert response.status_code == 200
    assert response.json()["mfa_required"] is True
    assert "access_token" not in response.json()
    assert (
        await client.get(
            "/api/v1/superadmin/me",
            headers={"Authorization": f"Bearer {response.json()['challenge_token']}"},
        )
    ).status_code == 401


async def test_AC0042_encrypted_enrollment_and_single_use(
    client, db, superadmin_account
):
    payload = {k: superadmin_account[k] for k in ("email", "password")}
    challenge = (
        await client.post("/api/v1/auth/superadmin/login", json=payload)
    ).json()["challenge_token"]
    enrollment = (
        await client.post(
            "/api/v1/auth/superadmin/mfa/totp/enroll",
            json={"challenge_token": challenge},
        )
    ).json()
    assert set(enrollment) == {"provisioning_uri"}
    secret = parse_qs(urlsplit(enrollment["provisioning_uri"]).query)["secret"][0]
    factor = await db.scalar(select(UserMfaCredential))
    assert factor.totp_secret != secret and decrypt_secret(factor.totp_secret) == secret
    data = {
        "challenge_token": challenge,
        "code": totp_at(secret, int(datetime.now(UTC).timestamp()) // 30),
    }
    response = await client.post("/api/v1/auth/superadmin/mfa/totp/confirm", json=data)
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "superadmin"
    claims = decode_token(response.json()["access_token"], "access")
    assert claims["mfa"] is True and claims["sid"] and "session_version" in claims
    assert (
        await client.post("/api/v1/auth/superadmin/mfa/totp/confirm", json=data)
    ).status_code == 401
    challenge2 = (
        await client.post("/api/v1/auth/superadmin/login", json=payload)
    ).json()["challenge_token"]
    response = await client.post(
        "/api/v1/auth/superadmin/mfa/verify",
        json={**data, "challenge_token": challenge2},
    )
    assert (
        response.status_code == 401
        and response.json()["code"] == "AUTH_MFA_CODE_REUSED"
    )


async def _admin(db, make_user):
    user = await make_user("target-admin@example.com")
    group = await iam_service.create_group(db, name=ADMIN_GROUP)
    await iam_service.add_user_to_group(db, user.id, group.id)
    return user


async def test_AC0043_AC0048_override_has_one_atomic_record(
    client, db, superadmin_headers, make_user
):
    target = await _admin(db, make_user)
    start = await db.scalar(select(func.count()).select_from(AuditEntry))
    response = await client.post(
        "/api/v1/superadmin/overrides/users.deactivate_admin",
        headers=superadmin_headers,
        json={"target_id": str(target.id), "justification": "Security incident"},
    )
    assert response.status_code == 200, response.text
    await db.refresh(target)
    assert not target.is_active
    assert await db.scalar(select(func.count()).select_from(AuditEntry)) == start + 1
    row = await db.scalar(
        select(AuditEntry).where(AuditEntry.action == "superadmin.override")
    )
    assert row.actor_id and row.occurred_at and row.target_id == str(target.id)
    assert row.prior_state == {"is_active": True} and row.new_state == {
        "is_active": False
    }
    assert row.payload["justification"] == "Security incident"


@pytest.mark.parametrize("reason", [None, "", "   "])
async def test_AC0044_blank_reason_changes_nothing(
    client, db, superadmin_headers, make_user, reason
):
    target = await _admin(db, make_user)
    response = await client.post(
        "/api/v1/superadmin/overrides/users.deactivate_admin",
        headers=superadmin_headers,
        json={"target_id": str(target.id), "justification": reason},
    )
    assert (
        response.status_code == 422
        and response.json()["code"] == "OVERRIDE_JUSTIFICATION_REQUIRED"
    )
    await db.refresh(target)
    assert target.is_active


async def test_AC0045_admin_cannot_manage_even_with_grants(
    client, db, auth_headers, registered_user, grant
):
    from cbpupsis_api_admin.domains.admin_auth import service as admin_service
    from cbpupsis_database.models.admin_auth import AdminPosition

    user_id = uuid.UUID(registered_user["id"])
    group = await iam_service.create_group(db, name=ADMIN_GROUP)
    await iam_service.add_user_to_group(db, user_id, group.id)
    await admin_service.configure_admin_profile(
        db, user_id=user_id, position=AdminPosition.registrar
    )
    response = await client.post(
        "/api/v1/auth/admin/login",
        json={
            "email": registered_user["email"],
            "password": registered_user["password"],
        },
    )
    challenge = response.json()["challenge_token"]
    response = await client.post(
        "/api/v1/auth/admin/mfa/totp/enroll", json={"challenge_token": challenge}
    )
    secret = response.json()["secret"]
    response = await client.post(
        "/api/v1/auth/admin/mfa/totp/confirm",
        json={
            "challenge_token": challenge,
            "code": totp_at(secret, int(datetime.now(UTC).timestamp()) // 30),
        },
    )
    assert response.status_code == 200, response.text
    auth_headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    await grant(uuid.UUID(registered_user["id"]), "ManageIAM")
    for path in ("/api/v1/superadmin/me", "/api/v1/iam/groups", "/api/v1/users"):
        assert (await client.get(path, headers=auth_headers)).status_code == 403
    assert (
        await client.patch(
            "/api/v1/audit/00000000-0000-0000-0000-000000000001",
            headers=auth_headers,
            json={},
        )
    ).status_code in (404, 405)


async def test_AC0046_distinct_second_person(
    client, db, superadmin_headers, registered_user, make_user
):
    backup = SystemBackup(
        backup_type="full", byte_size=10, status="completed", storage_location="test"
    )
    db.add(backup)
    await db.commit()
    path = f"/api/v1/superadmin/backups/{backup.id}/restore-requests"
    assert (await client.post(path, headers=superadmin_headers)).status_code == 202
    response = await client.post(path + "/approve", headers=superadmin_headers)
    assert (
        response.status_code == 403
        and response.json()["code"] == "DUAL_AUTH_SAME_ACTOR"
    )
    await db.refresh(backup)
    assert backup.restore_status == "pending"
    second = await make_user("second@example.com")
    await users_service.mark_email_verified(db, second.id)
    group = await db.scalar(select(Group).where(Group.name == SUPERADMIN_GROUP))
    await iam_service.add_user_to_group(db, second.id, group.id)
    sid = await auth_service.stage_superadmin_session(
        db, user_id=second.id, ip_address="127.0.0.1", device_info="test"
    )
    pair = await auth_service.issue_session(
        db, second.id, claims={"role": "superadmin", "mfa": True, "sid": str(sid)}
    )
    response = await client.post(
        path + "/approve", headers={"Authorization": f"Bearer {pair.access_token}"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["approved_by"] == str(second.id)


async def test_AC0047_idle_removes_session_and_refresh(client, db, superadmin_headers):
    session = await db.scalar(select(UserActiveSession))
    sid = session.id
    session.last_activity_at = datetime.now(UTC) - timedelta(minutes=15)
    await db.commit()
    response = await client.get("/api/v1/users/me", headers=superadmin_headers)
    assert (
        response.status_code == 401
        and response.json()["code"] == "AUTH_SESSION_EXPIRED"
    )
    assert await db.get(UserActiveSession, sid) is None
    assert all(row.revoked_at for row in await db.scalars(select(RefreshToken)))


async def test_AC0048_read_and_legacy_mutation_each_audited_once(
    client, db, superadmin_headers
):
    before = await db.scalar(select(func.count()).select_from(AuditEntry))
    assert (
        await client.get("/api/v1/users/me", headers=superadmin_headers)
    ).status_code == 200
    assert (
        await client.patch(
            "/api/v1/users/me",
            headers=superadmin_headers,
            json={"full_name": "Changed"},
        )
    ).status_code == 200
    assert await db.scalar(select(func.count()).select_from(AuditEntry)) == before + 2


async def test_user_creation_is_protected_and_signup_survives(
    client, db, superadmin_headers
):
    data = {
        "email": "new@example.com",
        "password": "a-long-enough-password",
        "full_name": "New",
    }
    assert (await client.post("/api/v1/users", json=data)).status_code == 401
    response = await client.post("/api/v1/users", headers=superadmin_headers, json=data)
    assert response.status_code == 201, response.text
    assert (
        response.json()["email_verified_at"] is None and "password" not in response.text
    )
    assert (
        await client.post("/api/v1/users", headers=superadmin_headers, json=data)
    ).status_code == 409
    assert (
        await client.post(
            "/api/v1/auth/register", json={**data, "email": "public@example.com"}
        )
    ).status_code == 201


async def test_override_rolls_back_when_audit_fails(
    client, db, superadmin_headers, make_user, monkeypatch
):
    from cbpupsis_shared.domains.audit import client as audit_client

    target = await _admin(db, make_user)
    target_id = target.id

    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(audit_client, "stage", fail)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await client.post(
            "/api/v1/superadmin/overrides/users.deactivate_admin",
            headers=superadmin_headers,
            json={"target_id": str(target_id), "justification": "Incident"},
        )
    assert (await users_service.get_by_id(db, target_id)).is_active


async def test_promotion_kills_old_tokens_and_role_conflicts(
    db, client, auth_headers, registered_user
):
    group = await iam_service.create_group(db, name=SUPERADMIN_GROUP)
    await iam_service.add_user_to_group(db, uuid.UUID(registered_user["id"]), group.id)
    assert (
        await client.get("/api/v1/users/me", headers=auth_headers)
    ).status_code == 401
    admin = await iam_service.create_group(db, name=ADMIN_GROUP)
    from cbpupsis_core.exceptions import ConflictError

    with pytest.raises(ConflictError):
        await iam_service.add_user_to_group(
            db, uuid.UUID(registered_user["id"]), admin.id
        )


async def test_AC0047_idle_refresh_cannot_revive_session(client, db, superadmin_tokens):
    session = await db.scalar(select(UserActiveSession))
    sid = session.id
    session.last_activity_at = datetime.now(UTC) - timedelta(minutes=15)
    await db.commit()
    response = await client.post(
        "/api/v1/auth/superadmin/refresh",
        json={"refresh_token": superadmin_tokens["refresh_token"]},
    )
    assert (
        response.status_code == 401
        and response.json()["code"] == "AUTH_SESSION_EXPIRED"
    )
    assert await db.get(UserActiveSession, sid) is None


async def test_refresh_rotation_and_logout(client, db, superadmin_tokens):
    assert (
        await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": superadmin_tokens["refresh_token"]},
        )
    ).status_code == 401
    response = await client.post(
        "/api/v1/auth/superadmin/refresh",
        json={"refresh_token": superadmin_tokens["refresh_token"]},
    )
    assert response.status_code == 200, response.text
    pair = response.json()
    assert pair["refresh_token"] != superadmin_tokens["refresh_token"]
    assert (
        await client.post(
            "/api/v1/auth/superadmin/logout",
            json={"refresh_token": pair["refresh_token"]},
        )
    ).status_code == 204
    assert (
        await client.get(
            "/api/v1/users/me",
            headers={"Authorization": f"Bearer {pair['access_token']}"},
        )
    ).status_code == 401
    assert list(await db.scalars(select(UserActiveSession))) == []


async def test_self_deactivation_is_atomic_and_audited_once(
    client, db, superadmin_headers
):
    before = await db.scalar(select(func.count()).select_from(AuditEntry))
    response = await client.post(
        "/api/v1/users/me/deactivate", headers=superadmin_headers
    )
    assert response.status_code == 204, response.text
    assert list(await db.scalars(select(UserActiveSession))) == []
    assert await db.scalar(select(func.count()).select_from(AuditEntry)) == before + 1


async def test_password_change_replaces_session_without_downgrading(
    client, db, superadmin_headers, registered_user
):
    old_sid = (await db.scalar(select(UserActiveSession))).id
    response = await client.post(
        "/api/v1/auth/change-password",
        headers=superadmin_headers,
        json={
            "current_password": registered_user["password"],
            "new_password": "ChangedPassword123!",
        },
    )
    assert response.status_code == 200, response.text
    claims = decode_token(response.json()["access_token"], "access")
    assert claims["role"] == "superadmin" and claims["sid"] != str(old_sid)
    assert await db.get(UserActiveSession, old_sid) is None
    assert (
        await client.get(
            "/api/v1/superadmin/me",
            headers={"Authorization": f"Bearer {response.json()['access_token']}"},
        )
    ).status_code == 200
    assert (
        await client.get("/api/v1/users/me", headers=superadmin_headers)
    ).status_code == 401


async def test_password_reset_revokes_superadmin_and_pending_challenge(
    client, db, superadmin_headers, registered_user, sent_emails, drain_outbox
):
    from cbpupsis_database.models.admin_auth import SuperadminChallenge
    from tests.conftest import token_from_email

    response = await client.post(
        "/api/v1/auth/superadmin/login",
        json={
            "email": registered_user["email"],
            "password": registered_user["password"],
        },
    )
    challenge = response.json()["challenge_token"]
    assert (
        await db.get(SuperadminChallenge, uuid.UUID(registered_user["id"])) is not None
    )
    sent_emails.clear()
    await client.post(
        "/api/v1/auth/forgot-password", json={"email": registered_user["email"]}
    )
    await drain_outbox()
    reset = next(mail for mail in sent_emails if "reset" in mail["subject"].lower())
    response = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": token_from_email(reset), "new_password": "ResetPassword123!"},
    )
    assert response.status_code == 200, response.text
    assert list(await db.scalars(select(UserActiveSession))) == []
    assert (
        await client.get("/api/v1/users/me", headers=superadmin_headers)
    ).status_code == 401
    response = await client.post(
        "/api/v1/auth/superadmin/mfa/verify",
        json={"challenge_token": challenge, "code": "123456"},
    )
    assert response.status_code == 401


async def test_expired_access_still_removes_idle_session(
    client, db, superadmin_headers
):
    import jwt

    from cbpupsis_core.config import settings

    claims = decode_token(superadmin_headers["Authorization"].split()[1], "access")
    claims["exp"] = int(datetime.now(UTC).timestamp()) - 1
    token = jwt.encode(
        claims, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    session = await db.scalar(select(UserActiveSession))
    session.last_activity_at = datetime.now(UTC) - timedelta(minutes=15)
    await db.commit()
    response = await client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert (
        response.status_code == 401
        and response.json()["code"] == "AUTH_SESSION_EXPIRED"
    )
    assert list(await db.scalars(select(UserActiveSession))) == []


async def test_role_removal_ends_session(
    client, db, superadmin_headers, registered_user
):
    group = await db.scalar(select(Group).where(Group.name == SUPERADMIN_GROUP))
    await iam_service.remove_user_from_group(
        db, uuid.UUID(registered_user["id"]), group.id
    )
    assert list(await db.scalars(select(UserActiveSession))) == []
    assert (
        await client.get("/api/v1/users/me", headers=superadmin_headers)
    ).status_code == 401


async def test_AC0041_rolling_lockout(client, superadmin_account):
    for _ in range(5):
        response = await client.post(
            "/api/v1/auth/superadmin/login",
            json={"email": superadmin_account["email"], "password": "wrong-password"},
        )
        assert response.status_code == 401
    response = await client.post(
        "/api/v1/auth/superadmin/login",
        json={
            "email": superadmin_account["email"],
            "password": superadmin_account["password"],
        },
    )
    assert (
        response.status_code == 423 and response.json()["code"] == "AUTH_ACCOUNT_LOCKED"
    )


async def test_superadmin_bootstrap_is_idempotent(db, registered_user, monkeypatch):
    from scripts.bootstrap_superadmin import bootstrap_superadmin
    from tests.core.test_bootstrap import _Session

    await iam_service.create_group(db, name=SUPERADMIN_GROUP)
    monkeypatch.setattr(
        "scripts.bootstrap_superadmin.AsyncSessionLocal", lambda: _Session(db)
    )
    assert await bootstrap_superadmin(registered_user["email"]) == 0
    user = await users_service.get_by_id(db, uuid.UUID(registered_user["id"]))
    version = user.session_version
    assert await bootstrap_superadmin(registered_user["email"]) == 0
    assert user.session_version == version
