"""Initial AC-004 checks for the Superadmin backend contract."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_admin.domains.admin_auth.security import decrypt_secret, totp_at
from cbpupsis_api_admin.domains.superadmin_ops import service as superadmin_service
from cbpupsis_database.models.admin_auth import MfaCredential
from cbpupsis_database.models.audit import AuditEntry
from cbpupsis_database.models.auth import ActiveSession
from cbpupsis_database.models.iam import Group
from cbpupsis_database.models.superadmin_ops import SystemBackup
from cbpupsis_shared.domains.audit import client as audit_client
from cbpupsis_shared.domains.auth.security import create_access_token, decode_token
from cbpupsis_shared.domains.iam import service as iam_service
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP, SUPERADMIN_GROUP
from cbpupsis_shared.domains.users import service as users_service


async def _admin_target(db: AsyncSession, make_user) -> uuid.UUID:
    user = await make_user("admin-target@example.com")
    group = await iam_service.create_group(db, name=ADMIN_GROUP)
    await iam_service.add_user_to_group(db, user.id, group.id)
    return user.id


async def _second_superadmin(
    client: AsyncClient, db: AsyncSession, make_user
) -> dict[str, str]:
    user = await make_user("second-superadmin@example.com")
    await users_service.mark_email_verified(db, user.id)
    group = await db.scalar(select(Group).where(Group.name == SUPERADMIN_GROUP))
    assert group is not None
    await iam_service.add_user_to_group(db, user.id, group.id)
    login = await client.post(
        "/api/v1/auth/superadmin/login",
        json={"email": user.email, "password": "a-long-enough-password"},
    )
    assert login.status_code == 200, login.text
    challenge = login.json()["challenge_token"]
    enrollment = await client.post(
        "/api/v1/auth/superadmin/mfa/totp/enroll",
        json={"challenge_token": challenge},
    )
    assert enrollment.status_code == 200, enrollment.text
    code = totp_at(
        enrollment.json()["secret"], int(datetime.now(UTC).timestamp()) // 30
    )
    confirmed = await client.post(
        "/api/v1/auth/superadmin/mfa/totp/confirm",
        json={"challenge_token": challenge, "code": code},
    )
    assert confirmed.status_code == 200, confirmed.text
    return {"Authorization": f"Bearer {confirmed.json()['access_token']}"}


async def test_AC0041_credentials_return_only_a_challenge(
    client: AsyncClient, db: AsyncSession, registered_user: dict[str, str]
) -> None:
    group = await iam_service.create_group(db, name=SUPERADMIN_GROUP)
    await iam_service.add_user_to_group(db, uuid.UUID(registered_user["id"]), group.id)
    response = await client.post(
        "/api/v1/auth/superadmin/login",
        json={
            "email": registered_user["email"],
            "password": registered_user["password"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["mfa_required"] is True
    assert "challenge_token" in response.json()
    assert "access_token" not in response.json()
    ordinary = await client.post(
        "/api/v1/auth/login",
        json={
            "email": registered_user["email"],
            "password": registered_user["password"],
        },
    )
    assert ordinary.status_code == 401


async def test_AC0042_factor_issues_a_live_superadmin_session(
    client: AsyncClient, db: AsyncSession, superadmin_headers: dict[str, str]
) -> None:
    response = await client.get("/api/v1/superadmin/me", headers=superadmin_headers)
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "superadmin"
    credential = await db.scalar(select(MfaCredential))
    assert credential is not None and credential.totp_secret is not None
    assert credential.totp_secret != decrypt_secret(credential.totp_secret)


async def test_AC0043_AC0048_override_has_one_attributed_audit_row(
    client: AsyncClient, db: AsyncSession, superadmin_headers: dict[str, str], make_user
) -> None:
    target_id = await _admin_target(db, make_user)
    ordinary = await client.post(
        f"/api/v1/users/{target_id}/deactivate", headers=superadmin_headers
    )
    assert ordinary.status_code == 403
    response = await client.post(
        "/api/v1/superadmin/overrides/users.deactivate_admin",
        json={"target_id": str(target_id), "justification": "Emergency account hold"},
        headers=superadmin_headers,
    )
    assert response.status_code == 200, response.text
    assert (await users_service.get_by_id(db, target_id)).is_active is False
    rows = (
        (
            await db.execute(
                select(AuditEntry).where(
                    AuditEntry.action == "superadmin.override",
                    AuditEntry.target_id == str(target_id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_id is not None and row.occurred_at is not None
    assert row.prior_state == {"is_active": True}
    assert row.new_state == {"is_active": False}
    assert row.payload["rule_id"] == "users.deactivate_admin"
    assert row.payload["justification"] == "Emergency account hold"


async def test_AC0044_blank_override_reason_changes_no_target(
    client: AsyncClient, db: AsyncSession, superadmin_headers: dict[str, str], make_user
) -> None:
    target_id = await _admin_target(db, make_user)
    response = await client.post(
        "/api/v1/superadmin/overrides/users.deactivate_admin",
        json={"target_id": str(target_id), "justification": "  "},
        headers=superadmin_headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "OVERRIDE_JUSTIFICATION_REQUIRED"
    assert (await users_service.get_by_id(db, target_id)).is_active is True


async def test_AC0046_restore_needs_a_distinct_second_superadmin(
    client: AsyncClient, db: AsyncSession, superadmin_headers: dict[str, str], make_user
) -> None:
    second = await _second_superadmin(client, db, make_user)
    backup = SystemBackup(
        backup_type="full",
        byte_size=10,
        status="completed",
        storage_location="test://backup",
    )
    db.add(backup)
    await db.commit()
    requested = await client.post(
        f"/api/v1/superadmin/backups/{backup.id}/restore-requests",
        headers=superadmin_headers,
    )
    assert requested.status_code == 202, requested.text
    self_approval = await client.post(
        f"/api/v1/superadmin/backups/{backup.id}/restore-requests/approve",
        headers=superadmin_headers,
    )
    assert self_approval.status_code == 403, self_approval.text
    assert self_approval.json()["code"] == "DUAL_AUTH_SAME_ACTOR"
    assert backup.restore_status == "pending"
    approved = await client.post(
        f"/api/v1/superadmin/backups/{backup.id}/restore-requests/approve",
        headers=second,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert backup.restore_completed_at is None


async def test_AC0047_idle_session_is_deleted_and_refused(
    client: AsyncClient, db: AsyncSession, superadmin_headers: dict[str, str]
) -> None:
    token = superadmin_headers["Authorization"].split(" ", 1)[1]
    claims = decode_token(token, expected_type="access")
    session_id = uuid.UUID(claims["sid"])
    row = await db.get(ActiveSession, session_id)
    assert row is not None
    row.last_activity_at = datetime.now(UTC) - timedelta(minutes=16)
    await db.commit()
    response = await client.get("/api/v1/superadmin/me", headers=superadmin_headers)
    assert response.status_code == 401, response.text
    assert response.json()["code"] == "AUTH_SESSION_EXPIRED"
    assert await db.get(ActiveSession, session_id) is None


async def test_AC0045_admin_token_cannot_reach_superadmin_or_ledger(
    client: AsyncClient, db: AsyncSession, registered_user: dict[str, str]
) -> None:
    user_id = uuid.UUID(registered_user["id"])
    group = await iam_service.create_group(db, name=ADMIN_GROUP)
    await iam_service.add_user_to_group(db, user_id, group.id)
    token = create_access_token(user_id, claims={"role": "admin", "mfa": True})
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/api/v1/audit", headers=headers)).status_code == 403
    assert (
        await client.get("/api/v1/superadmin/me", headers=headers)
    ).status_code == 403


async def test_AC0043_role_membership_cannot_be_ambiguous(
    db: AsyncSession,
    superadmin_headers: dict[str, str],
    registered_user: dict[str, str],
) -> None:
    from cbpupsis_shared.domains.iam.exceptions import GroupRoleConflictError

    admin_group = await iam_service.create_group(db, name=ADMIN_GROUP)
    with pytest.raises(GroupRoleConflictError):
        await iam_service.add_user_to_group(
            db, uuid.UUID(registered_user["id"]), admin_group.id
        )


async def test_AC0043_promotion_invalidates_ordinary_tokens(
    client: AsyncClient, db: AsyncSession, registered_user: dict[str, str]
) -> None:
    ordinary = await client.post(
        "/api/v1/auth/login",
        json={
            "email": registered_user["email"],
            "password": registered_user["password"],
        },
    )
    assert ordinary.status_code == 200
    group = await iam_service.create_group(db, name=SUPERADMIN_GROUP)
    await iam_service.add_user_to_group(db, uuid.UUID(registered_user["id"]), group.id)
    stale = await client.get(
        "/api/v1/auth/whoami",
        headers={"Authorization": f"Bearer {ordinary.json()['access_token']}"},
    )
    assert stale.status_code == 403
    refreshed = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": ordinary.json()["refresh_token"]},
    )
    assert refreshed.status_code == 401


async def test_AC0047_refresh_cannot_revive_an_idle_session(
    client: AsyncClient,
    db: AsyncSession,
    superadmin_pair: dict[str, str],
) -> None:
    claims = decode_token(superadmin_pair["access_token"], expected_type="access")
    session_id = uuid.UUID(claims["sid"])
    session = await db.get(ActiveSession, session_id)
    assert session is not None
    session.last_activity_at = datetime.now(UTC) - timedelta(minutes=16)
    await db.commit()
    ordinary = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": superadmin_pair["refresh_token"]},
    )
    assert ordinary.status_code == 401
    dedicated = await client.post(
        "/api/v1/auth/superadmin/refresh",
        json={"refresh_token": superadmin_pair["refresh_token"]},
    )
    assert dedicated.status_code == 401
    assert dedicated.json()["code"] == "AUTH_SESSION_EXPIRED"
    assert await db.get(ActiveSession, session_id) is None


async def test_AC0047_password_change_preserves_only_the_superadmin_session(
    client: AsyncClient,
    db: AsyncSession,
    superadmin_pair: dict[str, str],
    registered_user: dict[str, str],
) -> None:
    old_claims = decode_token(superadmin_pair["access_token"], expected_type="access")
    before = await db.scalar(select(func.count()).select_from(AuditEntry))
    changed = await client.post(
        "/api/v1/auth/change-password",
        json={
            "current_password": registered_user["password"],
            "new_password": "a-new-long-enough-password",
        },
        headers={"Authorization": f"Bearer {superadmin_pair['access_token']}"},
    )
    assert changed.status_code == 200, changed.text
    claims = decode_token(changed.json()["access_token"], expected_type="access")
    assert claims["role"] == "superadmin"
    assert claims["mfa"] is True
    assert claims["sid"] == old_claims["sid"]
    assert (
        await client.get(
            "/api/v1/superadmin/me",
            headers={"Authorization": f"Bearer {changed.json()['access_token']}"},
        )
    ).status_code == 200
    after = await db.scalar(select(func.count()).select_from(AuditEntry))
    assert after == before + 2  # password change and the protected read


async def test_AC0048_protected_read_adds_exactly_one_ledger_row(
    client: AsyncClient, db: AsyncSession, superadmin_headers: dict[str, str]
) -> None:
    before = await db.scalar(select(func.count()).select_from(AuditEntry))
    response = await client.get("/api/v1/superadmin/me", headers=superadmin_headers)
    assert response.status_code == 200
    after = await db.scalar(select(func.count()).select_from(AuditEntry))
    assert after == before + 1


async def test_AC0048_generic_mutation_audit_is_transactional(
    client: AsyncClient,
    db: AsyncSession,
    superadmin_headers: dict[str, str],
    registered_user: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.UUID(registered_user["id"])
    before = await db.scalar(select(func.count()).select_from(AuditEntry))
    changed = await client.patch(
        "/api/v1/users/me",
        json={"bio": "Updated"},
        headers=superadmin_headers,
    )
    assert changed.status_code == 200, changed.text
    after = await db.scalar(select(func.count()).select_from(AuditEntry))
    assert after == before + 1

    def fail_stage(*args, **kwargs) -> None:
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(audit_client, "stage", fail_stage)
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await client.patch(
            "/api/v1/users/me",
            json={"bio": "Must roll back"},
            headers=superadmin_headers,
        )
    await db.rollback()
    assert (await users_service.get_by_id(db, user_id)).bio == "Updated"


async def test_AC0048_self_deactivation_ends_session_with_one_audit_row(
    client: AsyncClient,
    db: AsyncSession,
    superadmin_headers: dict[str, str],
) -> None:
    claims = decode_token(
        superadmin_headers["Authorization"].split(" ", 1)[1],
        expected_type="access",
    )
    before = await db.scalar(select(func.count()).select_from(AuditEntry))
    response = await client.post(
        "/api/v1/users/me/deactivate", headers=superadmin_headers
    )
    assert response.status_code == 204, response.text
    assert await db.get(ActiveSession, uuid.UUID(claims["sid"])) is None
    rows = (
        (
            await db.execute(
                select(AuditEntry).where(
                    AuditEntry.action == "superadmin.self_deactivated"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].new_state == {"is_active": False}
    assert (await db.scalar(select(func.count()).select_from(AuditEntry))) == before + 1


async def test_AC0048_self_delete_ends_session_with_one_audit_row(
    client: AsyncClient,
    db: AsyncSession,
    superadmin_headers: dict[str, str],
    registered_user: dict[str, str],
) -> None:
    claims = decode_token(
        superadmin_headers["Authorization"].split(" ", 1)[1],
        expected_type="access",
    )
    before = await db.scalar(select(func.count()).select_from(AuditEntry))
    response = await client.request(
        "DELETE",
        "/api/v1/users/me",
        json={"password": registered_user["password"]},
        headers=superadmin_headers,
    )
    assert response.status_code == 204, response.text
    assert await db.get(ActiveSession, uuid.UUID(claims["sid"])) is None
    rows = (
        (
            await db.execute(
                select(AuditEntry).where(AuditEntry.action == "superadmin.self_deleted")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].new_state == {"deleted": True}
    assert (await db.scalar(select(func.count()).select_from(AuditEntry))) == before + 1


async def test_AC0048_override_rolls_back_when_ledger_staging_fails(
    db: AsyncSession,
    superadmin_headers: dict[str, str],
    registered_user: dict[str, str],
    make_user,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = await _admin_target(db, make_user)

    def fail_stage(*args, **kwargs) -> None:
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(audit_client, "stage", fail_stage)
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await superadmin_service.perform_override(
            db,
            actor_id=uuid.UUID(registered_user["id"]),
            rule_id="users.deactivate_admin",
            target_id=target_id,
            justification="Emergency hold",
        )
    await db.rollback()
    assert (await users_service.get_by_id(db, target_id)).is_active is True


async def test_AC0046_repeated_restore_authorization_conflicts(
    client: AsyncClient, db: AsyncSession, superadmin_headers: dict[str, str]
) -> None:
    backup = SystemBackup(
        backup_type="full",
        byte_size=10,
        status="completed",
        storage_location="test://backup-repeat",
    )
    db.add(backup)
    await db.commit()
    path = f"/api/v1/superadmin/backups/{backup.id}/restore-requests"
    assert (await client.post(path, headers=superadmin_headers)).status_code == 202
    assert (await client.post(path, headers=superadmin_headers)).status_code == 409


async def test_AC0046_removed_requester_cannot_be_approved(
    client: AsyncClient,
    db: AsyncSession,
    superadmin_headers: dict[str, str],
    registered_user: dict[str, str],
    make_user,
) -> None:
    second = await _second_superadmin(client, db, make_user)
    backup = SystemBackup(
        backup_type="full",
        byte_size=10,
        status="completed",
        storage_location="test://backup-revoked-requester",
    )
    db.add(backup)
    await db.commit()
    path = f"/api/v1/superadmin/backups/{backup.id}/restore-requests"
    assert (await client.post(path, headers=superadmin_headers)).status_code == 202
    group = await db.scalar(select(Group).where(Group.name == SUPERADMIN_GROUP))
    await iam_service.remove_user_from_group(
        db, uuid.UUID(registered_user["id"]), group.id
    )
    denied = await client.post(f"{path}/approve", headers=second)
    assert denied.status_code == 409
    assert backup.restore_status == "pending"
