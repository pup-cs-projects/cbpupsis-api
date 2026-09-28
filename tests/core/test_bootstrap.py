"""Tests for the admin bootstrap — the grant the API cannot make itself.

On a fresh database every IAM endpoint requires ``ManageIAM``, which only an IAM
endpoint can grant. If this path breaks, a new deployment has no way to reach an
administrator, so it is worth locking down.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.auth import service as auth_service
from app.domains.iam import service as iam_service
from app.domains.users import service as users_service
from scripts.bootstrap_admin import ADMIN_GROUP, bootstrap_admin


async def _seed_admin_group(db: AsyncSession) -> None:
    """Minimal stand-in for seed_iam: the Admins group with ManageIAM."""
    await iam_service.create_permission(db, action="ManageIAM")
    policy = await iam_service.create_policy(
        db, name="IAMAdmin", permission_actions=["ManageIAM"]
    )
    group = await iam_service.create_group(db, name=ADMIN_GROUP)
    await iam_service.attach_policy_to_group(db, group.id, policy.id)


class TestBootstrapAdmin:
    async def test_promotes_a_registered_user(
        self, db: AsyncSession, monkeypatch
    ) -> None:
        await _seed_admin_group(db)
        user = await auth_service.register(
            db, email="admin@example.com", password="a-long-enough-password"
        )

        monkeypatch.setattr(
            "scripts.bootstrap_admin.AsyncSessionLocal", lambda: _Session(db)
        )
        assert await bootstrap_admin("admin@example.com") == 0

        granted = await iam_service.get_effective_permissions(db, user.id)
        assert "ManageIAM" in granted

    async def test_is_case_insensitive_on_email(
        self, db: AsyncSession, monkeypatch
    ) -> None:
        await _seed_admin_group(db)
        await auth_service.register(
            db, email="Mixed@Example.com", password="a-long-enough-password"
        )
        monkeypatch.setattr(
            "scripts.bootstrap_admin.AsyncSessionLocal", lambda: _Session(db)
        )
        assert await bootstrap_admin("MIXED@EXAMPLE.COM") == 0

    async def test_fails_clearly_for_an_unknown_user(
        self, db: AsyncSession, monkeypatch
    ) -> None:
        await _seed_admin_group(db)
        monkeypatch.setattr(
            "scripts.bootstrap_admin.AsyncSessionLocal", lambda: _Session(db)
        )
        assert await bootstrap_admin("nobody@example.com") == 1

    async def test_fails_clearly_when_the_group_is_missing(
        self, db: AsyncSession, monkeypatch
    ) -> None:
        """Seeding is a prerequisite; say so rather than failing obscurely."""
        await auth_service.register(
            db, email="early@example.com", password="a-long-enough-password"
        )
        monkeypatch.setattr(
            "scripts.bootstrap_admin.AsyncSessionLocal", lambda: _Session(db)
        )
        assert await bootstrap_admin("early@example.com") == 1

    async def test_is_idempotent(self, db: AsyncSession, monkeypatch) -> None:
        await _seed_admin_group(db)
        await auth_service.register(
            db, email="twice@example.com", password="a-long-enough-password"
        )
        monkeypatch.setattr(
            "scripts.bootstrap_admin.AsyncSessionLocal", lambda: _Session(db)
        )
        assert await bootstrap_admin("twice@example.com") == 0
        assert await bootstrap_admin("twice@example.com") == 0

    async def test_the_bootstrapped_admin_can_then_use_the_iam_api(
        self,
        db: AsyncSession,
        client: AsyncClient,
        monkeypatch,
        admin_session_headers,
    ) -> None:
        """The end-to-end point: after bootstrap, the circularity is broken and
        every further grant happens through the API."""
        await _seed_admin_group(db)
        registered = await client.post(
            "/api/v1/auth/register",
            json={"email": "root@example.com", "password": "a-long-enough-password"},
        )
        # Login requires a verified address; this test is about the IAM
        # bootstrap, so satisfy the gate directly.
        await users_service.mark_email_verified(db, uuid.UUID(registered.json()["id"]))
        login = await client.post(
            "/api/v1/auth/login",
            json={"email": "root@example.com", "password": "a-long-enough-password"},
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        # Locked out before bootstrap.
        before = await client.post(
            "/api/v1/iam/groups", json={"name": "Nope"}, headers=headers
        )
        assert before.status_code == 403

        monkeypatch.setattr(
            "scripts.bootstrap_admin.AsyncSessionLocal", lambda: _Session(db)
        )
        assert await bootstrap_admin("root@example.com") == 0

        # A completed administrative session is now authorized.
        headers = await admin_session_headers(uuid.UUID(registered.json()["id"]))
        after = await client.post(
            "/api/v1/iam/groups", json={"name": "Editors"}, headers=headers
        )
        assert after.status_code == 201


class _Session:
    """Wrap the test session so the script's `async with` does not close it."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> None:
        return None


def test_admin_group_name_matches_the_seed() -> None:
    """The script and the seed must name the same group, or bootstrap fails
    on a correctly-seeded database."""
    from scripts.seed_iam import GROUPS

    assert ADMIN_GROUP in GROUPS
