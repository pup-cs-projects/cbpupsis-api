"""Tests for the users domain.

Services are framework-agnostic, so most of this exercises business logic with
no HTTP involved — one of the payoffs of the layering.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.domains.auth import service as auth_service
from app.domains.users import client as users_client
from app.domains.users import service as users_service


class TestUserService:
    async def test_get_by_id_returns_the_user(self, db: AsyncSession) -> None:
        created = await auth_service.register(
            db, email="a@example.com", password="a-long-enough-password"
        )
        assert (await users_service.get_by_id(db, created.id)).id == created.id

    async def test_missing_user_raises_not_found(self, db: AsyncSession) -> None:
        with pytest.raises(NotFoundError):
            await users_service.get_by_id(db, uuid.uuid4())

    async def test_get_by_email_returns_none_when_absent(
        self, db: AsyncSession
    ) -> None:
        """None, not an exception: login must not distinguish a missing account
        from a wrong password."""
        assert await users_service.get_by_email(db, "nobody@example.com") is None

    async def test_update_profile(self, db: AsyncSession) -> None:
        user = await auth_service.register(
            db, email="b@example.com", password="a-long-enough-password"
        )
        updated = await users_service.update_profile(db, user.id, full_name="Renamed")
        assert updated.full_name == "Renamed"

    async def test_deactivate_hides_from_active_lookup(self, db: AsyncSession) -> None:
        user = await auth_service.register(
            db, email="c@example.com", password="a-long-enough-password"
        )
        assert await users_service.get_active_user(db, user.id) is not None

        await users_service.deactivate(db, user.id)
        assert await users_service.get_active_user(db, user.id) is None
        # Still retrievable by id — deactivated, not deleted.
        assert await users_service.get_by_id(db, user.id) is not None


class TestUsersClient:
    """The cross-domain seam: other domains use this, never service.py."""

    async def test_client_returns_a_dto_not_an_orm_object(
        self, db: AsyncSession
    ) -> None:
        """An ORM object crossing a domain boundary would carry a session and a
        table dependency that a service split cannot preserve."""
        from app.domains.users.models import User
        from app.domains.users.schemas import UserRead

        created = await auth_service.register(
            db, email="d@example.com", password="a-long-enough-password"
        )
        result = await users_client.get_user(db, created.id)

        assert isinstance(result, UserRead)
        assert not isinstance(result, User)

    async def test_user_exists_checks_liveness(self, db: AsyncSession) -> None:
        """The service-layer integrity check that replaces a cross-domain FK."""
        user = await auth_service.register(
            db, email="e@example.com", password="a-long-enough-password"
        )
        assert await users_client.user_exists(db, user.id)
        assert not await users_client.user_exists(db, uuid.uuid4())

        await users_service.deactivate(db, user.id)
        assert not await users_client.user_exists(db, user.id)


class TestUserEndpoints:
    async def test_me_returns_the_caller(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
    ) -> None:
        response = await client.get("/api/v1/users/me", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["email"] == registered_user["email"]

    async def test_me_never_exposes_the_password_hash(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        body = (await client.get("/api/v1/users/me", headers=auth_headers)).json()
        assert "password_hash" not in body
        assert "password" not in body

    async def test_patch_me_updates_the_profile(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.patch(
            "/api/v1/users/me", json={"full_name": "New Name"}, headers=auth_headers
        )
        assert response.status_code == 200
        assert response.json()["full_name"] == "New Name"

    async def test_patch_me_rejects_unknown_fields(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """extra='forbid': a typo must error rather than be silently dropped."""
        response = await client.patch(
            "/api/v1/users/me", json={"is_active": False}, headers=auth_headers
        )
        assert response.status_code == 422

    async def test_reading_another_user_requires_permission(
        self, client: AsyncClient, db: AsyncSession, auth_headers: dict[str, str]
    ) -> None:
        other = await auth_service.register(
            db, email="other@example.com", password="a-long-enough-password"
        )
        response = await client.get(f"/api/v1/users/{other.id}", headers=auth_headers)
        assert response.status_code == 403

    async def test_reading_another_user_allowed_with_permission(
        self,
        client: AsyncClient,
        db: AsyncSession,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        grant,
    ) -> None:
        await grant(uuid.UUID(registered_user["id"]), "ReadAllUser")
        other = await auth_service.register(
            db, email="other2@example.com", password="a-long-enough-password"
        )
        response = await client.get(f"/api/v1/users/{other.id}", headers=auth_headers)
        assert response.status_code == 200

    async def test_reading_yourself_by_id_needs_no_permission(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
    ) -> None:
        """Self-access is the ownership half of the two-level check."""
        response = await client.get(
            f"/api/v1/users/{registered_user['id']}", headers=auth_headers
        )
        assert response.status_code == 200


class TestUserAdministration:
    """`GET /users` and the deactivate/reactivate pair.

    Every other `/users` route except `GET /{user_id}` is self-service, so these
    are the first that let one account act on another. The refusals matter more
    than the happy paths: an ungated user listing hands over every address in
    the database and returns a perfectly healthy 200.
    """

    @pytest.fixture
    async def reader_headers(
        self,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        admin_session_headers,
        grant,
    ) -> dict[str, str]:
        await grant(uuid.UUID(registered_user["id"]), "ReadAllUser")
        return await admin_session_headers(uuid.UUID(registered_user["id"]))

    @pytest.fixture
    async def admin_headers(
        self,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        admin_session_headers,
        grant,
    ) -> dict[str, str]:
        await grant(uuid.UUID(registered_user["id"]), "ReadAllUser")
        await grant(uuid.UUID(registered_user["id"]), "ManageUser")
        return await admin_session_headers(uuid.UUID(registered_user["id"]))

    async def test_list_is_refused_without_read_all_user(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.get("/api/v1/users", headers=auth_headers)
        assert response.status_code == 403, response.text

    async def test_list_is_refused_without_authentication(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/api/v1/users")
        assert response.status_code == 401, response.text

    async def test_list_returns_the_page_envelope(
        self, client: AsyncClient, reader_headers: dict[str, str]
    ) -> None:
        response = await client.get("/api/v1/users", headers=reader_headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert {"items", "total", "limit", "offset"} <= set(body)
        assert body["total"] >= 1

    async def test_list_never_exposes_the_password_hash(
        self, client: AsyncClient, reader_headers: dict[str, str]
    ) -> None:
        """The read schema is the boundary; this is the assertion that keeps it
        one after someone adds a field to UserRead."""
        response = await client.get("/api/v1/users", headers=reader_headers)
        assert response.status_code == 200, response.text
        for row in response.json()["items"]:
            assert "password_hash" not in row

    async def test_filters_by_active(
        self,
        client: AsyncClient,
        db: AsyncSession,
        reader_headers: dict[str, str],
        make_user,
    ) -> None:
        suspended = await make_user("suspended@example.com")
        await users_service.deactivate(db, suspended.id)

        active = await client.get(
            "/api/v1/users?is_active=true", headers=reader_headers
        )
        inactive = await client.get(
            "/api/v1/users?is_active=false", headers=reader_headers
        )
        assert active.status_code == 200, active.text
        assert inactive.status_code == 200, inactive.text

        active_ids = [row["id"] for row in active.json()["items"]]
        inactive_ids = [row["id"] for row in inactive.json()["items"]]
        assert str(suspended.id) in inactive_ids
        assert str(suspended.id) not in active_ids

    async def test_filters_by_verified(
        self,
        client: AsyncClient,
        db: AsyncSession,
        reader_headers: dict[str, str],
        registered_user: dict[str, str],
        make_user,
    ) -> None:
        unverified = await make_user("unverified@example.com")

        verified = await client.get(
            "/api/v1/users?is_verified=true", headers=reader_headers
        )
        pending = await client.get(
            "/api/v1/users?is_verified=false", headers=reader_headers
        )
        assert verified.status_code == 200, verified.text
        assert pending.status_code == 200, pending.text

        verified_ids = [row["id"] for row in verified.json()["items"]]
        pending_ids = [row["id"] for row in pending.json()["items"]]
        assert registered_user["id"] in verified_ids
        assert str(unverified.id) in pending_ids
        assert str(unverified.id) not in verified_ids

    async def test_omitting_a_filter_returns_both_states(
        self,
        client: AsyncClient,
        db: AsyncSession,
        reader_headers: dict[str, str],
        make_user,
    ) -> None:
        """Tri-state: the filter is absent, not defaulted to true."""
        suspended = await make_user("suspended@example.com")
        await users_service.deactivate(db, suspended.id)

        response = await client.get("/api/v1/users", headers=reader_headers)
        ids = [row["id"] for row in response.json()["items"]]
        assert str(suspended.id) in ids

    async def test_deactivate_is_refused_without_manage_user(
        self,
        client: AsyncClient,
        reader_headers: dict[str, str],
        make_user,
    ) -> None:
        """ReadAllUser alone must not confer the power to suspend an account."""
        target = await make_user("target@example.com")

        response = await client.post(
            f"/api/v1/users/{target.id}/deactivate", headers=reader_headers
        )
        assert response.status_code == 403, response.text

    async def test_reactivate_is_refused_without_manage_user(
        self, client: AsyncClient, reader_headers: dict[str, str], make_user
    ) -> None:
        target = await make_user("target@example.com")

        response = await client.post(
            f"/api/v1/users/{target.id}/reactivate", headers=reader_headers
        )
        assert response.status_code == 403, response.text

    async def test_admin_deactivates_and_reactivates(
        self,
        client: AsyncClient,
        db: AsyncSession,
        admin_headers: dict[str, str],
        make_user,
    ) -> None:
        target = await make_user("target@example.com")

        deactivated = await client.post(
            f"/api/v1/users/{target.id}/deactivate", headers=admin_headers
        )
        assert deactivated.status_code == 200, deactivated.text
        assert deactivated.json()["is_active"] is False

        reactivated = await client.post(
            f"/api/v1/users/{target.id}/reactivate", headers=admin_headers
        )
        assert reactivated.status_code == 200, reactivated.text
        assert reactivated.json()["is_active"] is True

    async def test_admin_cannot_deactivate_themselves(
        self,
        client: AsyncClient,
        admin_headers: dict[str, str],
        registered_user: dict[str, str],
    ) -> None:
        """No undo through the API: reactivating needs the permission the
        caller would just have cut themselves off from."""
        response = await client.post(
            f"/api/v1/users/{registered_user['id']}/deactivate",
            headers=admin_headers,
        )
        assert response.status_code == 422, response.text

    async def test_there_is_no_admin_delete_route(
        self, client: AsyncClient, admin_headers: dict[str, str], make_user
    ) -> None:
        """Deletion stays self-service and password-confirmed.

        An administrator cannot supply the password that authorizes it, so the
        route deliberately does not exist — 405, because /users/{id} matches
        only GET.
        """
        target = await make_user("target@example.com")

        response = await client.delete(
            f"/api/v1/users/{target.id}", headers=admin_headers
        )
        assert response.status_code == 405, response.text

    async def test_deactivating_revokes_the_targets_sessions(
        self,
        client: AsyncClient,
        db: AsyncSession,
        admin_headers: dict[str, str],
        make_user,
    ) -> None:
        """Suspension must take effect now, not when a token expires."""
        target = await make_user("target@example.com")
        await users_service.mark_email_verified(db, target.id)
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "target@example.com",
                "password": "a-long-enough-password",
            },
        )
        assert login.status_code == 200, login.text
        target_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        assert (
            await client.get("/api/v1/users/me", headers=target_headers)
        ).status_code == 200

        await client.post(
            f"/api/v1/users/{target.id}/deactivate", headers=admin_headers
        )

        after = await client.get("/api/v1/users/me", headers=target_headers)
        assert after.status_code == 401, after.text
