"""Tests for the authentication flows.

The security-relevant behaviours here — rotation, reuse detection, token-type
confusion, enumeration resistance — are the ones most likely to regress
silently, because a broken version still passes a naive happy-path test.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.exceptions import UnauthorizedError
from cbpupsis_shared.domains.auth import service as auth_service
from cbpupsis_shared.domains.auth.security import (
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from cbpupsis_shared.domains.users import service as users_service


class TestPasswordHashing:
    def test_hash_is_not_the_password(self) -> None:
        hashed = hash_password("correct-horse-battery-staple")
        assert hashed != "correct-horse-battery-staple"
        assert hashed.startswith("$2b$")
        assert int(hashed.split("$")[2]) >= 12

    def test_verify_accepts_correct_and_rejects_wrong(self) -> None:
        hashed = hash_password("correct-horse-battery-staple")
        assert verify_password("correct-horse-battery-staple", hashed)
        assert not verify_password("wrong-password", hashed)

    def test_same_password_hashes_differently(self) -> None:
        """Distinct salts: identical passwords must not produce identical hashes,
        or a leaked table reveals which users share a password."""
        assert hash_password("same-password-here") != hash_password(
            "same-password-here"
        )


class TestRegistration:
    async def test_register_returns_user_without_password(
        self, client: AsyncClient
    ) -> None:
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "new@example.com",
                "password": "a-sufficiently-long-password",
                "full_name": "New User",
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["email"] == "new@example.com"
        assert "password" not in body
        assert "password_hash" not in body

    async def test_duplicate_email_conflicts(
        self, client: AsyncClient, registered_user: dict[str, str]
    ) -> None:
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": registered_user["email"],
                "password": "another-long-enough-password",
            },
        )
        assert response.status_code == 409

    async def test_short_password_rejected(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": "short@example.com", "password": "tiny"},
        )
        assert response.status_code == 422


class TestLogin:
    async def test_login_returns_token_pair(
        self, client: AsyncClient, registered_user: dict[str, str]
    ) -> None:
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["access_token"] and body["refresh_token"]
        assert body["token_type"] == "bearer"

    async def test_wrong_password_and_unknown_email_are_indistinguishable(
        self, client: AsyncClient, registered_user: dict[str, str]
    ) -> None:
        """Both must return the same status and message, or the endpoint becomes
        an oracle for which addresses are registered."""
        wrong_password = await client.post(
            "/api/v1/auth/login",
            json={"email": registered_user["email"], "password": "not-the-password"},
        )
        unknown_email = await client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "not-the-password"},
        )
        assert wrong_password.status_code == unknown_email.status_code == 401
        assert wrong_password.json()["detail"] == unknown_email.json()["detail"]


class TestTokens:
    async def test_refresh_token_rejected_as_access_token(
        self, client: AsyncClient, registered_user: dict[str, str]
    ) -> None:
        """Type confusion: a refresh token must not authenticate API calls."""
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        refresh_token = login.json()["refresh_token"]
        response = await client.get(
            "/api/v1/auth/whoami",
            headers={"Authorization": f"Bearer {refresh_token}"},
        )
        assert response.status_code == 401

    async def test_tampered_token_rejected(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        tampered = auth_headers["Authorization"][:-3] + "aaa"
        response = await client.get(
            "/api/v1/auth/whoami", headers={"Authorization": tampered}
        )
        assert response.status_code == 401

    async def test_missing_token_rejected(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/auth/whoami")).status_code == 401

    def test_decode_rejects_wrong_type(self) -> None:
        import uuid

        token, _, _ = create_refresh_token(uuid.uuid4())
        with pytest.raises(UnauthorizedError):
            decode_token(token, expected_type="access")


class TestRefreshRotation:
    async def test_refresh_returns_a_new_pair(
        self, client: AsyncClient, registered_user: dict[str, str]
    ) -> None:
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        original = login.json()["refresh_token"]

        response = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": original}
        )
        assert response.status_code == 200
        assert response.json()["refresh_token"] != original

    async def test_reusing_a_rotated_token_fails(
        self, client: AsyncClient, registered_user: dict[str, str]
    ) -> None:
        """The core of rotation: the old token must die when it is exchanged."""
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        original = login.json()["refresh_token"]
        await client.post("/api/v1/auth/refresh", json={"refresh_token": original})

        replay = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": original}
        )
        assert replay.status_code == 401

    async def test_reuse_revokes_the_whole_family(
        self, client: AsyncClient, registered_user: dict[str, str]
    ) -> None:
        """Replaying a rotated token implies theft, so the newly issued token is
        revoked too — the attacker and the victim both lose the session."""
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        original = login.json()["refresh_token"]

        rotated = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": original}
        )
        current = rotated.json()["refresh_token"]

        # Attacker replays the stolen (already rotated) token.
        await client.post("/api/v1/auth/refresh", json={"refresh_token": original})

        # The legitimate token issued a moment ago is now dead as well.
        response = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": current}
        )
        assert response.status_code == 401

    async def test_logout_revokes_the_token(
        self, client: AsyncClient, registered_user: dict[str, str]
    ) -> None:
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        refresh_token = login.json()["refresh_token"]

        assert (
            await client.post(
                "/api/v1/auth/logout", json={"refresh_token": refresh_token}
            )
        ).status_code == 204
        assert (
            await client.post(
                "/api/v1/auth/refresh", json={"refresh_token": refresh_token}
            )
        ).status_code == 401

    async def test_logout_is_idempotent(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/auth/logout", json={"refresh_token": "not-even-a-jwt"}
        )
        assert response.status_code == 204


class TestDeactivation:
    async def test_deactivated_user_loses_access_immediately(
        self,
        client: AsyncClient,
        db: AsyncSession,
        registered_user: dict[str, str],
        auth_headers: dict[str, str],
    ) -> None:
        """The token is still cryptographically valid; access must stop anyway,
        which is why the user row is loaded on every request."""
        import uuid

        from cbpupsis_shared.domains.users import service as users_service

        assert (
            await client.get("/api/v1/auth/whoami", headers=auth_headers)
        ).status_code == 200

        await users_service.deactivate(db, uuid.UUID(registered_user["id"]))

        assert (
            await client.get("/api/v1/auth/whoami", headers=auth_headers)
        ).status_code == 401

    async def test_login_rejected_after_deactivation(
        self, client: AsyncClient, db: AsyncSession, registered_user: dict[str, str]
    ) -> None:
        import uuid

        from cbpupsis_shared.domains.users import service as users_service

        await users_service.deactivate(db, uuid.UUID(registered_user["id"]))
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        assert response.status_code == 401


class TestServiceLayer:
    """Services are framework-agnostic, so they test without HTTP."""

    async def test_register_and_login_directly(self, db: AsyncSession) -> None:
        user = await auth_service.register(
            db, email="Direct@Example.com", password="a-long-enough-password"
        )
        assert user.email == "direct@example.com", "email should be normalised"

        # Login now requires a verified address; this test is about the
        # register-then-login path, not the gate, so satisfy it and move on.
        await users_service.mark_email_verified(db, user.id)

        pair = await auth_service.login(
            db, email="direct@example.com", password="a-long-enough-password"
        )
        assert pair.access_token

    async def test_login_is_case_insensitive_on_email(self, db: AsyncSession) -> None:
        user = await auth_service.register(
            db, email="mixed@example.com", password="a-long-enough-password"
        )
        await users_service.mark_email_verified(db, user.id)

        pair = await auth_service.login(
            db, email="MIXED@example.com", password="a-long-enough-password"
        )
        assert pair.access_token
