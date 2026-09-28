"""US-AUTH-003 administrative MFA, position scope, and lockout acceptance tests."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import register_exception_handlers
from app.core.outbox import OutboxMessage
from app.database import get_db
from app.domains.audit import service as audit_service
from app.domains.auth import repository as auth_repository
from app.domains.auth import service as auth_service
from app.domains.auth.models import AdminPosition
from app.domains.auth.security import _totp_at, decode_token
from app.domains.iam import service as iam_service
from app.domains.iam.constants import ADMIN_GROUP
from app.domains.iam.dependencies import require_admin_scope
from app.domains.users import service as users_service
from app.domains.users.constants import MANAGE_USER

PASSWORD = "correct-horse-battery-staple"


async def _make_admin(
    client: AsyncClient,
    db: AsyncSession,
    *,
    email: str = "admin@example.com",
    position: AdminPosition = AdminPosition.chairperson,
):
    response = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    user_id = uuid.UUID(response.json()["id"])
    await users_service.mark_email_verified(db, user_id)
    group = await iam_service.create_group(db, name=ADMIN_GROUP)
    await iam_service.add_user_to_group(db, user_id, group.id)
    await auth_service.configure_admin_profile(
        db,
        user_id=user_id,
        position=position,
        department_id="CCIS" if position is AdminPosition.chairperson else None,
        college_id="COE" if position is AdminPosition.dean else None,
    )
    return user_id


async def _challenge(client: AsyncClient, email: str = "admin@example.com") -> dict:
    response = await client.post(
        "/api/v1/auth/admin/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _current_code(secret: str) -> str:
    return _totp_at(secret, int(datetime.now(UTC).timestamp()) // 30)


async def _enroll_totp(
    client: AsyncClient, challenge_token: str
) -> tuple[str, dict, str, str]:
    enrollment = await client.post(
        "/api/v1/auth/admin/mfa/totp/enroll",
        json={"challenge_token": challenge_token},
    )
    assert enrollment.status_code == 200, enrollment.text
    secret = enrollment.json()["secret"]
    code = _current_code(secret)
    confirmation = await client.post(
        "/api/v1/auth/admin/mfa/totp/confirm",
        json={"challenge_token": challenge_token, "code": code},
    )
    assert confirmation.status_code == 200, confirmation.text
    return secret, confirmation.json(), confirmation.headers["set-cookie"], code


class TestAdministrativeMfa:
    async def test_admin_credentials_are_refused_at_ordinary_login(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await _make_admin(client, db)

        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": PASSWORD},
        )

        assert response.status_code == 401
        assert "access_token" not in response.json()

    async def test_non_admin_credentials_are_refused_at_admin_login(
        self, client: AsyncClient, registered_user: dict[str, str]
    ) -> None:
        response = await client.post(
            "/api/v1/auth/admin/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )

        assert response.status_code == 401
        assert "challenge_token" not in response.json()

    async def test_ac_003_1_password_step_issues_only_a_challenge(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await _make_admin(client, db)

        body = await _challenge(client)

        assert body["mfa_required"] is True
        assert body["enrollment_required"] is True
        assert body["challenge_token"]
        assert "access_token" not in body
        assert "refresh_token" not in body

    async def test_ac_003_2_totp_creates_scoped_admin_session_and_secure_cookie(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        user_id = await _make_admin(client, db)
        challenge = await _challenge(client)

        secret, body, set_cookie, _ = await _enroll_totp(
            client, challenge["challenge_token"]
        )

        claims = decode_token(body["access_token"], expected_type="access")
        assert claims["sub"] == str(user_id)
        assert claims["role"] == "admin"
        assert claims["mfa"] is True
        assert claims["position"] == "chairperson"
        assert claims["department_id"] == "CCIS"
        assert body["role"] == "admin"
        assert body["position"] == "chairperson"

        profile = await auth_repository.get_admin_profile(db, user_id)
        assert profile is not None
        assert profile.totp_secret_encrypted != secret
        assert secret not in profile.totp_secret_encrypted

        cookie = client.cookies.get("admin_session")
        assert cookie == body["access_token"]
        assert "httponly" in set_cookie.lower()
        assert "secure" in set_cookie.lower()
        assert "samesite=lax" in set_cookie.lower()
        assert "path=/api/v1" in set_cookie.lower()

        replay = await client.post(
            "/api/v1/auth/admin/mfa/totp/confirm",
            json={
                "challenge_token": challenge["challenge_token"],
                "code": _current_code(secret),
            },
        )
        assert replay.status_code == 401

    async def test_ac_003_2_totp_code_is_single_use_across_fresh_challenges(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await _make_admin(client, db)
        enrollment = await _challenge(client)
        _, _, _, spent_code = await _enroll_totp(client, enrollment["challenge_token"])
        challenge = await _challenge(client)

        replay = await client.post(
            "/api/v1/auth/admin/mfa/verify",
            json={
                "challenge_token": challenge["challenge_token"],
                "code": spent_code,
            },
        )

        assert replay.status_code == 401
        assert replay.json()["code"] == "AUTH_MFA_CODE_REUSED"
        assert "access_token" not in replay.json()

    async def test_ac_003_3_invalid_factor_is_counted_and_issues_no_session(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await _make_admin(client, db)
        first = await _challenge(client)
        await _enroll_totp(client, first["challenge_token"])
        challenge = await _challenge(client)

        response = await client.post(
            "/api/v1/auth/admin/mfa/verify",
            json={"challenge_token": challenge["challenge_token"], "code": "000000"},
        )

        assert response.status_code == 401
        assert response.json()["code"] == "AUTH_MFA_INVALID"
        assert "access_token" not in response.json()
        claims = decode_token(challenge["challenge_token"], "mfa_challenge")
        assert (
            await auth_repository.count_authentication_failures_since(
                db,
                user_id=uuid.UUID(claims["sub"]),
                since=datetime.min.replace(tzinfo=UTC),
            )
            == 1
        )

    async def test_completed_admin_cookie_reaches_real_management_routes(
        self, client: AsyncClient, db: AsyncSession, grant
    ) -> None:
        user_id = await _make_admin(client, db)
        await grant(user_id, "ManageIAM")
        challenge = await _challenge(client)
        _, session, _, _ = await _enroll_totp(client, challenge["challenge_token"])

        response = await client.get(
            "/api/v1/iam/groups",
            headers={"Cookie": f"admin_session={session['access_token']}"},
        )

        assert response.status_code == 200, response.text

    async def test_admin_group_removal_revokes_a_completed_session_immediately(
        self, client: AsyncClient, db: AsyncSession, grant
    ) -> None:
        user_id = await _make_admin(client, db)
        await grant(user_id, "ManageIAM")
        challenge = await _challenge(client)
        _, session, _, _ = await _enroll_totp(client, challenge["challenge_token"])
        groups = await iam_service.list_groups(db, limit=200)
        admin_group = next(group for group in groups.items if group.name == ADMIN_GROUP)
        await iam_service.remove_user_from_group(db, user_id, admin_group.id)

        response = await client.get(
            "/api/v1/iam/groups",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )

        assert response.status_code == 403
        assert response.json()["code"] == "AUTH_INSUFFICIENT_ROLE"

    async def test_ac_003_4_real_admin_route_refuses_an_ordinary_session(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        grant,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await grant(uuid.UUID(registered_user["id"]), "ManageIAM")
        with caplog.at_level(logging.WARNING, logger="app.domains.auth.dependencies"):
            response = await client.get("/api/v1/iam/groups", headers=auth_headers)

        assert response.status_code == 403
        assert response.json()["code"] == "AUTH_INSUFFICIENT_ROLE"
        refusal = next(
            record
            for record in caplog.records
            if record.message == "admin.authorization_refused"
        )
        assert refusal.path == "/api/v1/iam/groups"
        assert refusal.status_code == 403
        assert refusal.error_code == "AUTH_INSUFFICIENT_ROLE"

    async def test_ac_003_5_scope_dependency_hides_resource_before_handler(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await _make_admin(client, db)
        challenge = await _challenge(client)
        _, session, _, _ = await _enroll_totp(client, challenge["challenge_token"])

        scope_app = FastAPI()
        register_exception_handlers(scope_app)
        scope_app.dependency_overrides[get_db] = lambda: db
        reached: list[str] = []

        @scope_app.get("/departments/{department_id}/records")
        async def scoped_records(
            department_id: str,
            _user=Depends(require_admin_scope),
        ) -> dict[str, str]:
            reached.append(department_id)
            return {"department_id": department_id}

        headers = {"Authorization": f"Bearer {session['access_token']}"}
        async with AsyncClient(
            transport=ASGITransport(app=scope_app), base_url="http://test"
        ) as scoped_client:
            denied = await scoped_client.get(
                "/departments/CAF/records", headers=headers
            )
            allowed = await scoped_client.get(
                "/departments/CCIS/records", headers=headers
            )

        assert denied.status_code == 404
        assert denied.json()["code"] == "RESOURCE_NOT_FOUND"
        assert reached == ["CCIS"]
        assert allowed.status_code == 200

    async def test_ac_003_6_unenrolled_challenge_reaches_only_enrollment(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await _make_admin(client, db)
        challenge = await _challenge(client)

        denied = await client.get(
            "/api/v1/iam/groups",
            headers={"Authorization": f"Bearer {challenge['challenge_token']}"},
        )
        enrolled = await client.post(
            "/api/v1/auth/admin/mfa/totp/enroll",
            json={"challenge_token": challenge["challenge_token"]},
        )

        assert denied.status_code == 403
        assert denied.json()["code"] == "AUTH_MFA_ENROLLMENT_REQUIRED"
        assert enrolled.status_code == 200

    async def test_ac_003_7_admin_action_writes_immutable_before_after_audit(
        self,
        client: AsyncClient,
        db: AsyncSession,
        grant,
        drain_outbox,
    ) -> None:
        actor_id = await _make_admin(client, db)
        await grant(actor_id, MANAGE_USER)
        challenge = await _challenge(client)
        _, session, _, _ = await _enroll_totp(client, challenge["challenge_token"])

        target = await client.post(
            "/api/v1/auth/register",
            json={"email": "target@example.com", "password": PASSWORD},
        )
        assert target.status_code == 201, target.text
        target_id = target.json()["id"]

        response = await client.post(
            f"/api/v1/users/{target_id}/deactivate",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )

        assert response.status_code == 200, response.text
        await drain_outbox()
        page = await audit_service.list_entries(db, action="user.deactivated")
        assert page.total == 1
        entry = page.items[0]
        assert entry.actor_id == actor_id
        assert entry.target_id == target_id
        assert entry.prior_state == {"is_active": True}
        assert entry.new_state == {"is_active": False}

        await db.delete(entry)
        with pytest.raises(PermissionError, match="append-only"):
            await db.commit()
        await db.rollback()

    async def test_admin_refresh_renews_cookie_and_logout_clears_it(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await _make_admin(client, db)
        challenge = await _challenge(client)
        _, session, _, _ = await _enroll_totp(client, challenge["challenge_token"])

        refreshed = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": session["refresh_token"]},
        )
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["role"] == "admin"
        assert "path=/api/v1" in refreshed.headers["set-cookie"].lower()
        assert client.cookies.get("admin_session") == refreshed.json()["access_token"]

        logged_out = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": refreshed.json()["refresh_token"]},
        )
        assert logged_out.status_code == 204, logged_out.text
        cookies = "\n".join(logged_out.headers.get_list("set-cookie")).lower()
        assert "admin_session=" in cookies
        assert "max-age=0" in cookies

    async def test_ac_003_8_lockout_counts_password_and_factor_failures_together(
        self, client: AsyncClient, db: AsyncSession
    ) -> None:
        await _make_admin(client, db)
        enrollment_challenge = await _challenge(client)
        await _enroll_totp(client, enrollment_challenge["challenge_token"])
        challenge = await _challenge(client)

        for _ in range(3):
            failed = await client.post(
                "/api/v1/auth/admin/login",
                json={"email": "admin@example.com", "password": "wrong-password"},
            )
            assert failed.status_code == 401
        for _ in range(2):
            failed = await client.post(
                "/api/v1/auth/admin/mfa/verify",
                json={
                    "challenge_token": challenge["challenge_token"],
                    "code": "000000",
                },
            )
            assert failed.status_code == 401

        sixth = await client.post(
            "/api/v1/auth/admin/login",
            json={"email": "admin@example.com", "password": PASSWORD},
        )
        assert sixth.status_code == 423
        assert sixth.json()["code"] == "AUTH_ACCOUNT_LOCKED"
        assert sixth.json()["retry_after_seconds"] == 900
        assert sixth.headers["Retry-After"] == "900"
        notices = (
            (
                await db.execute(
                    select(OutboxMessage).where(
                        OutboxMessage.event_name == "auth.account_locked"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(notices) == 1
        assert notices[0].payload["user_id"]


class TestHardwareKeyPath:
    async def test_ac_003_2_valid_hardware_assertion_uses_stored_counter(
        self, client: AsyncClient, db: AsyncSession, monkeypatch
    ) -> None:
        user_id = await _make_admin(
            client, db, email="key@example.com", position=AdminPosition.registrar
        )
        profile = await auth_service.configure_webauthn_factor(
            db,
            user_id=user_id,
            credential_id="credential-id",
            credential_public_key="public-key",
            sign_count=7,
        )
        assert profile.webauthn_credential_id_encrypted != "credential-id"
        assert profile.webauthn_public_key_encrypted != "public-key"
        monkeypatch.setattr(
            "app.domains.auth.service.verify_webauthn_assertion",
            lambda **_kwargs: 8,
        )
        challenge = await _challenge(client, email="key@example.com")

        response = await client.post(
            "/api/v1/auth/admin/mfa/verify",
            json={
                "challenge_token": challenge["challenge_token"],
                "assertion": {"id": "credential-id"},
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["role"] == "admin"
        assert response.json()["position"] == "registrar"
        await db.refresh(profile)
        assert profile.webauthn_sign_count == 8
