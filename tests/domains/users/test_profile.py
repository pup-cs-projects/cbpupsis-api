"""Tests for profile editing, onboarding, deactivation, and account deletion.

The account-lifecycle tests carry most of the weight: deactivation and deletion
are the two places where "the row still exists" and "the user still has access"
must come apart, and a partial implementation of either looks fine until someone
checks whether the old tokens still work.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.exceptions import UnauthorizedError, ValidationError
from cbpupsis_shared.domains.auth import service as auth_service
from cbpupsis_shared.domains.users import service as users_service


class TestProfileUpdate:
    async def test_updates_the_new_profile_fields(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.patch(
            "/api/v1/users/me",
            headers=auth_headers,
            json={
                "display_name": "JP",
                "bio": "Backend engineer.",
                "avatar_url": "https://cdn.example.com/a.png",
                "timezone": "Asia/Manila",
                "locale": "en-US",
                "phone_number": "+14155552671",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["display_name"] == "JP"
        assert body["bio"] == "Backend engineer."
        assert body["avatar_url"] == "https://cdn.example.com/a.png"
        assert body["timezone"] == "Asia/Manila"
        assert body["locale"] == "en-US"
        assert body["phone_number"] == "+14155552671"

    async def test_partial_update_leaves_other_fields_alone(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """The distinction between PATCH and PUT: an omitted field is untouched,
        not blanked."""
        await client.patch(
            "/api/v1/users/me",
            headers=auth_headers,
            json={"display_name": "Original", "timezone": "UTC"},
        )
        response = await client.patch(
            "/api/v1/users/me", headers=auth_headers, json={"bio": "Added later."}
        )
        assert response.status_code == 200
        assert response.json()["display_name"] == "Original"
        assert response.json()["timezone"] == "UTC"

    async def test_explicit_null_clears_a_field(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """Sending null is a deliberate clear, distinct from omitting the key."""
        await client.patch(
            "/api/v1/users/me", headers=auth_headers, json={"bio": "Temporary."}
        )
        response = await client.patch(
            "/api/v1/users/me", headers=auth_headers, json={"bio": None}
        )
        assert response.status_code == 200
        assert response.json()["bio"] is None

    async def test_rejects_unknown_fields(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """extra='forbid': a client typo must error, not vanish."""
        response = await client.patch(
            "/api/v1/users/me",
            headers=auth_headers,
            json={"displayName": "camelCase typo"},
        )
        assert response.status_code == 422

    async def test_cannot_set_server_owned_fields(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """Verification status is not a field a user may assert about itself."""
        response = await client.patch(
            "/api/v1/users/me",
            headers=auth_headers,
            json={"email_verified_at": "2020-01-01T00:00:00Z"},
        )
        assert response.status_code == 422

    async def test_email_cannot_be_changed_here(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """Changing a login identifier needs a verification flow, not a PATCH."""
        response = await client.patch(
            "/api/v1/users/me",
            headers=auth_headers,
            json={"email": "attacker@example.com"},
        )
        assert response.status_code == 422

    async def test_requires_authentication(self, client: AsyncClient) -> None:
        response = await client.patch("/api/v1/users/me", json={"display_name": "x"})
        assert response.status_code == 401

    @pytest.mark.parametrize(
        "payload",
        [
            {"timezone": "PST"},
            {"timezone": "Mars/Olympus"},
            {"locale": "not a locale!"},
            {"phone_number": "0123"},
            {"phone_number": "555-1234"},
            {"avatar_url": "javascript:alert(1)"},
            {"avatar_url": "/relative/path.png"},
            {"display_name": ""},
        ],
    )
    async def test_invalid_values_are_rejected(
        self, client: AsyncClient, auth_headers: dict[str, str], payload: dict
    ) -> None:
        """Validation at the edge. The javascript: case matters most — a stored
        value rendered into an href would be stored XSS."""
        response = await client.patch(
            "/api/v1/users/me", headers=auth_headers, json=payload
        )
        assert response.status_code == 422, payload


class TestOnboarding:
    async def test_incomplete_profile_is_rejected(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.post(
            "/api/v1/users/me/complete-onboarding", headers=auth_headers
        )
        assert response.status_code == 422
        assert "display_name" in str(response.json()["detail"])

    async def test_completes_once_required_fields_are_set(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        await client.patch(
            "/api/v1/users/me",
            headers=auth_headers,
            json={"display_name": "JP", "timezone": "Asia/Manila"},
        )
        response = await client.post(
            "/api/v1/users/me/complete-onboarding", headers=auth_headers
        )
        assert response.status_code == 200
        assert response.json()["onboarding_completed_at"] is not None

    async def test_is_idempotent_and_keeps_the_first_timestamp(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """The meaningful fact is when onboarding was first finished."""
        await client.patch(
            "/api/v1/users/me",
            headers=auth_headers,
            json={"display_name": "JP", "timezone": "Asia/Manila"},
        )
        first = await client.post(
            "/api/v1/users/me/complete-onboarding", headers=auth_headers
        )
        second = await client.post(
            "/api/v1/users/me/complete-onboarding", headers=auth_headers
        )
        assert second.status_code == 200
        assert (
            second.json()["onboarding_completed_at"]
            == first.json()["onboarding_completed_at"]
        )

    async def test_requires_authentication(self, client: AsyncClient) -> None:
        response = await client.post("/api/v1/users/me/complete-onboarding")
        assert response.status_code == 401

    async def test_service_raises_listing_what_is_missing(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        user = await auth_service.register(
            db, email="onboard@example.com", password="a-long-enough-password"
        )
        with pytest.raises(ValidationError) as excinfo:
            await users_service.complete_onboarding(db, user.id)
        assert "display_name" in str(excinfo.value)
        assert "timezone" in str(excinfo.value)


class TestDeactivateAccount:
    async def test_deactivate_returns_204(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.post(
            "/api/v1/users/me/deactivate", headers=auth_headers
        )
        assert response.status_code == 204

    async def test_access_token_stops_working(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        await client.post("/api/v1/users/me/deactivate", headers=auth_headers)
        response = await client.get("/api/v1/auth/whoami", headers=auth_headers)
        assert response.status_code == 401

    async def test_login_is_refused_after_deactivation(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
    ) -> None:
        await client.post("/api/v1/users/me/deactivate", headers=auth_headers)
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        assert response.status_code == 401

    async def test_refresh_tokens_are_revoked(
        self, client: AsyncClient, registered_user: dict[str, str]
    ) -> None:
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        pair = login.json()
        headers = {"Authorization": f"Bearer {pair['access_token']}"}

        await client.post("/api/v1/users/me/deactivate", headers=headers)

        response = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
        )
        assert response.status_code == 401

    async def test_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.post("/api/v1/users/me/deactivate")).status_code == 401

    async def test_deactivation_is_reversible(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        """Nothing is scrubbed, so the account comes back intact."""
        user = await auth_service.register(
            db, email="revive@example.com", password="a-long-enough-password"
        )
        await users_service.deactivate_own_account(db, user.id)
        assert await users_service.get_active_user(db, user.id) is None

        restored = await users_service.reactivate(db, user.id)
        assert restored.is_active
        assert restored.email == "revive@example.com"
        # Login also requires a verified address, which is orthogonal to
        # reactivation; stamp it so the assertion below tests what it claims to.
        await users_service.mark_email_verified(db, user.id)
        pair = await auth_service.login(
            db, email="revive@example.com", password="a-long-enough-password"
        )
        assert pair.access_token


class TestDeleteAccount:
    async def test_requires_the_correct_password(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """A stolen access token alone must not be able to delete an account."""
        response = await client.request(
            "DELETE",
            "/api/v1/users/me",
            headers=auth_headers,
            json={"password": "not-the-right-password"},
        )
        assert response.status_code == 401

    async def test_wrong_password_leaves_the_account_usable(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        await client.request(
            "DELETE",
            "/api/v1/users/me",
            headers=auth_headers,
            json={"password": "not-the-right-password"},
        )
        assert (
            await client.get("/api/v1/users/me", headers=auth_headers)
        ).status_code == 200

    async def test_deletes_with_the_correct_password(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
    ) -> None:
        response = await client.request(
            "DELETE",
            "/api/v1/users/me",
            headers=auth_headers,
            json={"password": registered_user["password"]},
        )
        assert response.status_code == 204

    async def test_access_dies_immediately(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
    ) -> None:
        await client.request(
            "DELETE",
            "/api/v1/users/me",
            headers=auth_headers,
            json={"password": registered_user["password"]},
        )
        assert (
            await client.get("/api/v1/auth/whoami", headers=auth_headers)
        ).status_code == 401

    async def test_refresh_tokens_are_dead(
        self, client: AsyncClient, registered_user: dict[str, str]
    ) -> None:
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        pair = login.json()
        await client.request(
            "DELETE",
            "/api/v1/users/me",
            headers={"Authorization": f"Bearer {pair['access_token']}"},
            json={"password": registered_user["password"]},
        )
        response = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
        )
        assert response.status_code == 401

    async def test_login_is_impossible_afterwards(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
    ) -> None:
        await client.request(
            "DELETE",
            "/api/v1/users/me",
            headers=auth_headers,
            json={"password": registered_user["password"]},
        )
        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered_user["email"],
                "password": registered_user["password"],
            },
        )
        assert response.status_code == 401

    async def test_requires_authentication(self, client: AsyncClient) -> None:
        response = await client.request(
            "DELETE", "/api/v1/users/me", json={"password": "anything-at-all"}
        )
        assert response.status_code == 401


class TestDeletionScrubsPII:
    """What survives deletion and what does not — the documented contract."""

    async def test_pii_is_scrubbed_but_the_row_survives(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        user = await auth_service.register(
            db,
            email="scrub@example.com",
            password="a-long-enough-password",
            full_name="Real Name",
        )
        await users_service.update_profile(
            db,
            user.id,
            display_name="Nickname",
            bio="About me.",
            avatar_url="https://cdn.example.com/me.png",
            phone_number="+14155552671",
            timezone="Asia/Manila",
            locale="en-US",
        )
        user_id = user.id

        await users_service.delete_own_account(db, user_id, "a-long-enough-password")

        # The row is still there: records referencing this id are not orphaned.
        from sqlalchemy import select

        from cbpupsis_database.models.users import User

        row = await db.scalar(select(User).where(User.id == user_id))
        assert row is not None
        assert row.id == user_id

        # ...and nothing identifying survives on it.
        assert row.full_name is None
        assert row.display_name is None
        assert row.bio is None
        assert row.avatar_url is None
        assert row.phone_number is None
        assert row.timezone is None
        assert row.locale is None
        assert "scrub@example.com" not in row.email
        assert row.email.endswith("@invalid")

        assert row.deleted_at is not None
        assert row.anonymized_at is not None
        assert row.is_active is False

    async def test_password_hash_is_destroyed(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        """A stale argon2 digest of a reused password is a liability with no
        remaining purpose."""
        from sqlalchemy import select

        from cbpupsis_database.models.users import User
        from cbpupsis_shared.domains.auth.security import verify_password

        user = await auth_service.register(
            db, email="hash@example.com", password="a-long-enough-password"
        )
        original_hash = user.password_hash

        await users_service.delete_own_account(db, user.id, "a-long-enough-password")

        row = await db.scalar(select(User).where(User.id == user.id))
        assert row is not None
        assert row.password_hash != original_hash
        assert not verify_password("a-long-enough-password", row.password_hash)

    async def test_login_against_a_deleted_account_401s_rather_than_500s(
        self, client: AsyncClient, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        """The scrubbed hash is not a parseable digest, so a naive
        ``verify_password`` raises and the endpoint 500s. A 500 where every
        other address gives 401 is itself an oracle for deleted accounts."""
        user = await auth_service.register(
            db, email="oracle@example.com", password="a-long-enough-password"
        )
        await users_service.delete_own_account(db, user.id, "a-long-enough-password")

        deleted = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "oracle@example.com",
                "password": "a-long-enough-password",
            },
        )
        never_existed = await client.post(
            "/api/v1/auth/login",
            json={"email": "never@example.com", "password": "a-long-enough-password"},
        )
        assert deleted.status_code == never_existed.status_code == 401
        assert deleted.json()["detail"] == never_existed.json()["detail"]

    async def test_verify_password_is_false_for_a_scrubbed_hash(self) -> None:
        from cbpupsis_shared.domains.auth.security import verify_password

        assert verify_password("anything-at-all", "!deleted") is False

    async def test_deleted_user_is_gone_from_every_lookup(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        user = await auth_service.register(
            db, email="gone@example.com", password="a-long-enough-password"
        )
        await users_service.delete_own_account(db, user.id, "a-long-enough-password")

        from cbpupsis_core.exceptions import NotFoundError

        assert await users_service.get_active_user(db, user.id) is None
        assert await users_service.get_by_email(db, "gone@example.com") is None
        with pytest.raises(NotFoundError):
            await users_service.get_by_id(db, user.id)

    async def test_the_freed_email_can_register_again(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        """The placeholder email is why: leaving the address in place would make
        the unique index reject the new signup."""
        user = await auth_service.register(
            db, email="reuse@example.com", password="a-long-enough-password"
        )
        await users_service.delete_own_account(db, user.id, "a-long-enough-password")

        fresh = await auth_service.register(
            db, email="reuse@example.com", password="a-different-long-password"
        )
        assert fresh.id != user.id

    async def test_two_deletions_do_not_collide(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        """The placeholder embeds the user id, so the unique index holds."""
        first = await auth_service.register(
            db, email="one@example.com", password="a-long-enough-password"
        )
        second = await auth_service.register(
            db, email="two@example.com", password="a-long-enough-password"
        )
        await users_service.delete_own_account(db, first.id, "a-long-enough-password")
        await users_service.delete_own_account(db, second.id, "a-long-enough-password")

        from sqlalchemy import select

        from cbpupsis_database.models.users import User

        emails = (await db.execute(select(User.email))).scalars().all()
        assert len(emails) == len(set(emails))

    async def test_a_deleted_account_cannot_be_reactivated(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        """Restoring one would hand back an account with no usable credential."""
        user = await auth_service.register(
            db, email="norevive@example.com", password="a-long-enough-password"
        )
        await users_service.delete_own_account(db, user.id, "a-long-enough-password")
        with pytest.raises((ValidationError, Exception)):
            await users_service.reactivate(db, user.id)

    async def test_wrong_password_raises_and_changes_nothing(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        user = await auth_service.register(
            db, email="safe@example.com", password="a-long-enough-password"
        )
        with pytest.raises(UnauthorizedError):
            await users_service.delete_own_account(db, user.id, "wrong-password")

        assert await users_service.get_active_user(db, user.id) is not None


class TestProfileService:
    async def test_update_profile_rejects_non_editable_fields(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        """The allow-list means a new column is not writable by accident."""
        user = await auth_service.register(
            db, email="guard@example.com", password="a-long-enough-password"
        )
        with pytest.raises(ValueError, match="Not editable"):
            await users_service.update_profile(db, user.id, is_active=False)

    async def test_update_profile_is_still_partial_at_the_service_layer(
        self, db: AsyncSession, sent_emails: list[dict[str, str]]
    ) -> None:
        user = await auth_service.register(
            db, email="partial@example.com", password="a-long-enough-password"
        )
        await users_service.update_profile(db, user.id, display_name="Kept")
        updated = await users_service.update_profile(db, user.id, bio="Only this")

        assert updated.display_name == "Kept"
        assert updated.bio == "Only this"

    async def test_unknown_user_raises_not_found(self, db: AsyncSession) -> None:
        from cbpupsis_core.exceptions import NotFoundError

        with pytest.raises(NotFoundError):
            await users_service.update_profile(db, uuid.uuid4(), bio="x")
