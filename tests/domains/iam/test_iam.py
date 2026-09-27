"""Tests for authorization: permission resolution and the endpoint guards."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError
from app.domains.auth.dependencies import CurrentUser
from app.domains.iam import service as iam_service
from app.domains.iam.dependencies import require_any_permission, require_permission


class TestEffectivePermissions:
    async def test_user_with_no_grants_has_none(self, db: AsyncSession) -> None:
        assert await iam_service.get_effective_permissions(db, uuid.uuid4()) == set()

    async def test_permissions_resolve_through_the_group_path(
        self, db: AsyncSession, make_user
    ) -> None:
        """user -> group -> policy -> permission, the primary grant path."""
        user_id = (await make_user("group-path@example.com")).id
        await iam_service.create_permission(db, action="ReadThing")
        await iam_service.create_permission(db, action="WriteThing")
        policy = await iam_service.create_policy(
            db, name="ThingAccess", permission_actions=["ReadThing", "WriteThing"]
        )
        group = await iam_service.create_group(db, name="Things")
        await iam_service.attach_policy_to_group(db, group.id, policy.id)
        await iam_service.add_user_to_group(db, user_id, group.id)

        granted = await iam_service.get_effective_permissions(db, user_id)
        assert granted == {"ReadThing", "WriteThing"}

    async def test_permissions_resolve_through_the_direct_path(
        self, db: AsyncSession
    ) -> None:
        user_id = uuid.uuid4()
        await iam_service.create_permission(db, action="DirectAction")
        policy = await iam_service.create_policy(
            db, name="DirectPolicy", permission_actions=["DirectAction"]
        )
        await iam_service.attach_policy_to_user(db, user_id, policy.id)

        assert await iam_service.get_effective_permissions(db, user_id) == {
            "DirectAction"
        }

    async def test_both_paths_union_without_duplicates(
        self, db: AsyncSession, make_user
    ) -> None:
        user_id = (await make_user("both-paths@example.com")).id
        await iam_service.create_permission(db, action="Shared")
        await iam_service.create_permission(db, action="GroupOnly")

        group_policy = await iam_service.create_policy(
            db, name="GroupPolicy", permission_actions=["Shared", "GroupOnly"]
        )
        direct_policy = await iam_service.create_policy(
            db, name="DirectPolicy", permission_actions=["Shared"]
        )
        group = await iam_service.create_group(db, name="Overlap")
        await iam_service.attach_policy_to_group(db, group.id, group_policy.id)
        await iam_service.add_user_to_group(db, user_id, group.id)
        await iam_service.attach_policy_to_user(db, user_id, direct_policy.id)

        assert await iam_service.get_effective_permissions(db, user_id) == {
            "Shared",
            "GroupOnly",
        }

    async def test_multiple_groups_union(self, db: AsyncSession, make_user) -> None:
        """A user in several groups inherits the union, not the intersection."""
        user_id = (await make_user("multiple-groups@example.com")).id
        for action in ("AlphaAction", "BetaAction"):
            await iam_service.create_permission(db, action=action)
            policy = await iam_service.create_policy(
                db, name=f"{action}Policy", permission_actions=[action]
            )
            group = await iam_service.create_group(db, name=f"{action}Group")
            await iam_service.attach_policy_to_group(db, group.id, policy.id)
            await iam_service.add_user_to_group(db, user_id, group.id)

        assert await iam_service.get_effective_permissions(db, user_id) == {
            "AlphaAction",
            "BetaAction",
        }

    async def test_revoking_group_membership_takes_effect_at_once(
        self, db: AsyncSession, make_user
    ) -> None:
        """Permissions are resolved per request, so removal is immediate — the
        reason they must never be baked into a token."""
        user_id = (await make_user("revoked-member@example.com")).id
        await iam_service.create_permission(db, action="Temporary")
        policy = await iam_service.create_policy(
            db, name="TempPolicy", permission_actions=["Temporary"]
        )
        group = await iam_service.create_group(db, name="TempGroup")
        await iam_service.attach_policy_to_group(db, group.id, policy.id)
        await iam_service.add_user_to_group(db, user_id, group.id)
        assert "Temporary" in await iam_service.get_effective_permissions(db, user_id)

        await iam_service.remove_user_from_group(db, user_id, group.id)
        assert await iam_service.get_effective_permissions(db, user_id) == set()

    async def test_policy_with_unknown_permission_is_rejected(
        self, db: AsyncSession
    ) -> None:
        """Fail loudly rather than silently creating a policy that grants less
        than its author intended."""
        with pytest.raises(NotFoundError):
            await iam_service.create_policy(
                db, name="Broken", permission_actions=["NoSuchAction"]
            )


class TestGuards:
    async def test_require_permission_allows_and_denies(
        self, db: AsyncSession, grant
    ) -> None:
        user_id = uuid.uuid4()
        user = CurrentUser(id=user_id, email="guard@example.com")
        await grant(user_id, "AllowedAction")

        checker = require_permission("AllowedAction")
        assert await checker(user=user, db=db) is user

        denied = require_permission("OtherAction")
        with pytest.raises(ForbiddenError):
            await denied(user=user, db=db)

    async def test_require_permission_needs_all_of_them(
        self, db: AsyncSession, grant
    ) -> None:
        user_id = uuid.uuid4()
        user = CurrentUser(id=user_id, email="guard@example.com")
        await grant(user_id, "FirstAction")

        checker = require_permission("FirstAction", "SecondAction")
        with pytest.raises(ForbiddenError):
            await checker(user=user, db=db)

    async def test_require_any_permission_needs_only_one(
        self, db: AsyncSession, grant
    ) -> None:
        user_id = uuid.uuid4()
        user = CurrentUser(id=user_id, email="guard@example.com")
        await grant(user_id, "OneOfThem")

        checker = require_any_permission("OneOfThem", "NotGranted")
        assert await checker(user=user, db=db) is user

        with pytest.raises(ForbiddenError):
            await require_any_permission("NeitherA", "NeitherB")(user=user, db=db)


class TestManagementEndpoints:
    async def test_management_requires_manage_iam(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """An authenticated but unprivileged user cannot reshape authorization."""
        response = await client.post(
            "/api/v1/iam/groups",
            json={"name": "Sneaky"},
            headers=auth_headers,
        )
        assert response.status_code == 403

    async def test_management_allowed_with_permission(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        grant,
    ) -> None:
        await grant(uuid.UUID(registered_user["id"]), "ManageIAM")
        response = await client.post(
            "/api/v1/iam/groups",
            json={"name": "Legitimate"},
            headers=auth_headers,
        )
        assert response.status_code == 201
        assert response.json()["name"] == "Legitimate"

    async def test_unauthenticated_management_is_rejected(
        self, client: AsyncClient
    ) -> None:
        response = await client.post("/api/v1/iam/groups", json={"name": "Anon"})
        assert response.status_code == 401

    async def test_unknown_user_cannot_be_added_to_a_group(
        self,
        client: AsyncClient,
        db: AsyncSession,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        grant,
    ) -> None:
        await grant(uuid.UUID(registered_user["id"]), "ManageIAM")
        group = await iam_service.create_group(db, name="NoGhostMembers")

        response = await client.post(
            f"/api/v1/iam/groups/{group.id}/users",
            json={"user_id": str(uuid.UUID(int=0))},
            headers=auth_headers,
        )

        assert response.status_code == 404, response.text


class TestPolicySchema:
    async def test_policy_read_includes_its_permissions(self, db: AsyncSession) -> None:
        """PolicyRead advertises a permissions list; it must actually populate,
        not silently serialise as an empty array."""
        from app.domains.iam.schemas import PolicyRead

        await iam_service.create_permission(db, action="AlphaPerm")
        await iam_service.create_permission(db, action="BetaPerm")
        policy = await iam_service.create_policy(
            db, name="Bundle", permission_actions=["AlphaPerm", "BetaPerm"]
        )

        dto = PolicyRead.model_validate(policy)
        assert {p.action for p in dto.permissions} == {"AlphaPerm", "BetaPerm"}


class TestIAMReadEndpointsRequireManageIAM:
    """Every new read endpoint is gated.

    The management API reveals the whole authorization model, so an ungated GET
    is an information leak even though it writes nothing: it tells an attacker
    exactly which permissions exist and who holds them.
    """

    #: Every endpoint on the new read/detach surface, so one cannot be added
    #: without a refusal test. Parametrised rather than copied, so this list is
    #: the checklist.
    ENDPOINTS = (
        ("get", "/api/v1/iam/permissions"),
        ("get", "/api/v1/iam/permissions/1"),
        ("get", "/api/v1/iam/policies"),
        ("get", "/api/v1/iam/policies/1"),
        ("get", "/api/v1/iam/groups"),
        ("get", "/api/v1/iam/groups/1"),
        ("get", "/api/v1/iam/users/11111111-1111-1111-1111-111111111111/permissions"),
        ("delete", "/api/v1/iam/groups/1/policies/1"),
        ("delete", "/api/v1/iam/groups/1/users/11111111-1111-1111-1111-111111111111"),
    )

    @pytest.mark.parametrize("method,path", ENDPOINTS)
    async def test_refused_without_manage_iam(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        method: str,
        path: str,
    ) -> None:
        response = await getattr(client, method)(path, headers=auth_headers)
        assert response.status_code == 403, response.text

    @pytest.mark.parametrize("method,path", ENDPOINTS)
    async def test_refused_without_authentication(
        self, client: AsyncClient, method: str, path: str
    ) -> None:
        response = await getattr(client, method)(path)
        assert response.status_code == 401, response.text


class TestIAMReadEndpoints:
    @pytest.fixture
    async def admin_headers(
        self,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        grant,
    ) -> dict[str, str]:
        await grant(uuid.UUID(registered_user["id"]), "ManageIAM")
        return auth_headers

    async def test_lists_permissions_in_the_page_envelope(
        self, client: AsyncClient, db: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        await iam_service.create_permission(db, action="ReadWidget")

        response = await client.get("/api/v1/iam/permissions", headers=admin_headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert {"items", "total", "limit", "offset"} <= set(body)
        assert "ReadWidget" in [row["action"] for row in body["items"]]

    async def test_reads_one_permission(
        self, client: AsyncClient, db: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        permission = await iam_service.create_permission(db, action="ReadWidget")

        response = await client.get(
            f"/api/v1/iam/permissions/{permission.id}", headers=admin_headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["action"] == "ReadWidget"

    async def test_unknown_permission_is_404(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.get(
            "/api/v1/iam/permissions/99999", headers=admin_headers
        )
        assert response.status_code == 404, response.text

    async def test_lists_policies_with_their_permissions(
        self, client: AsyncClient, db: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        await iam_service.create_permission(db, action="ReadWidget")
        await iam_service.create_policy(
            db, name="WidgetReader", permission_actions=["ReadWidget"]
        )

        response = await client.get("/api/v1/iam/policies", headers=admin_headers)
        assert response.status_code == 200, response.text
        policy = next(
            row for row in response.json()["items"] if row["name"] == "WidgetReader"
        )
        assert [p["action"] for p in policy["permissions"]] == ["ReadWidget"]

    async def test_reads_one_policy(
        self, client: AsyncClient, db: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        await iam_service.create_permission(db, action="ReadWidget")
        policy = await iam_service.create_policy(
            db, name="WidgetReader", permission_actions=["ReadWidget"]
        )

        response = await client.get(
            f"/api/v1/iam/policies/{policy.id}", headers=admin_headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["name"] == "WidgetReader"

    async def test_unknown_policy_is_404(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.get("/api/v1/iam/policies/99999", headers=admin_headers)
        assert response.status_code == 404, response.text

    async def test_lists_and_reads_groups(
        self, client: AsyncClient, db: AsyncSession, admin_headers: dict[str, str]
    ) -> None:
        group = await iam_service.create_group(db, name="Widgeteers")

        listed = await client.get("/api/v1/iam/groups", headers=admin_headers)
        assert listed.status_code == 200, listed.text
        assert "Widgeteers" in [row["name"] for row in listed.json()["items"]]

        detail = await client.get(
            f"/api/v1/iam/groups/{group.id}", headers=admin_headers
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["name"] == "Widgeteers"

    async def test_unknown_group_is_404(
        self, client: AsyncClient, admin_headers: dict[str, str]
    ) -> None:
        response = await client.get("/api/v1/iam/groups/99999", headers=admin_headers)
        assert response.status_code == 404, response.text

    async def test_reports_a_users_effective_permissions_by_source(
        self,
        client: AsyncClient,
        db: AsyncSession,
        admin_headers: dict[str, str],
        make_user,
    ) -> None:
        """Both grant paths are reported separately, and the union is what the
        guards resolve against."""
        subject = (await make_user("permission-subject@example.com")).id
        await iam_service.create_permission(db, action="ReadWidget")
        await iam_service.create_permission(db, action="ModerateWidget")
        group_policy = await iam_service.create_policy(
            db, name="WidgetReader", permission_actions=["ReadWidget"]
        )
        direct_policy = await iam_service.create_policy(
            db, name="WidgetModerator", permission_actions=["ModerateWidget"]
        )
        group = await iam_service.create_group(db, name="Widgeteers")
        await iam_service.attach_policy_to_group(db, group.id, group_policy.id)
        await iam_service.add_user_to_group(db, subject, group.id)
        await iam_service.attach_policy_to_user(db, subject, direct_policy.id)

        response = await client.get(
            f"/api/v1/iam/users/{subject}/permissions", headers=admin_headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["from_groups"] == ["ReadWidget"]
        assert body["from_direct"] == ["ModerateWidget"]
        assert body["effective"] == ["ModerateWidget", "ReadWidget"]


class TestDetachingRevokesAccess:
    """A detach that returns 204 without revoking anything looks identical to
    one that works. These assert the effective permission, not the status."""

    async def test_detaching_a_policy_from_a_group_removes_the_permission(
        self, db: AsyncSession, make_user
    ) -> None:
        user_id = (await make_user("detach-policy@example.com")).id
        await iam_service.create_permission(db, action="ReadWidget")
        policy = await iam_service.create_policy(
            db, name="WidgetReader", permission_actions=["ReadWidget"]
        )
        group = await iam_service.create_group(db, name="Widgeteers")
        await iam_service.attach_policy_to_group(db, group.id, policy.id)
        await iam_service.add_user_to_group(db, user_id, group.id)
        granted = await iam_service.get_effective_permissions(db, user_id)
        assert granted == {"ReadWidget"}

        await iam_service.detach_policy_from_group(db, group.id, policy.id)

        assert await iam_service.get_effective_permissions(db, user_id) == set()

    async def test_detaching_is_idempotent(self, db: AsyncSession) -> None:
        """Removing an edge that is not there is a no-op, not an error."""
        group = await iam_service.create_group(db, name="Widgeteers")
        await iam_service.detach_policy_from_group(db, group.id, 4242)

    async def test_detach_over_http_revokes_the_permission(
        self,
        client: AsyncClient,
        db: AsyncSession,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        grant,
        make_user,
    ) -> None:
        await grant(uuid.UUID(registered_user["id"]), "ManageIAM")

        subject = (await make_user("http-detach-policy@example.com")).id
        await iam_service.create_permission(db, action="ReadWidget")
        policy = await iam_service.create_policy(
            db, name="WidgetReader", permission_actions=["ReadWidget"]
        )
        group = await iam_service.create_group(db, name="Widgeteers")
        await iam_service.attach_policy_to_group(db, group.id, policy.id)
        await iam_service.add_user_to_group(db, subject, group.id)

        response = await client.delete(
            f"/api/v1/iam/groups/{group.id}/policies/{policy.id}",
            headers=auth_headers,
        )
        assert response.status_code == 204, response.text
        assert await iam_service.get_effective_permissions(db, subject) == set()

    async def test_removing_a_user_from_a_group_over_http_revokes_access(
        self,
        client: AsyncClient,
        db: AsyncSession,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        grant,
        make_user,
    ) -> None:
        await grant(uuid.UUID(registered_user["id"]), "ManageIAM")

        subject = (await make_user("http-remove-member@example.com")).id
        await iam_service.create_permission(db, action="ReadWidget")
        policy = await iam_service.create_policy(
            db, name="WidgetReader", permission_actions=["ReadWidget"]
        )
        group = await iam_service.create_group(db, name="Widgeteers")
        await iam_service.attach_policy_to_group(db, group.id, policy.id)
        await iam_service.add_user_to_group(db, subject, group.id)

        response = await client.delete(
            f"/api/v1/iam/groups/{group.id}/users/{subject}", headers=auth_headers
        )
        assert response.status_code == 204, response.text
        assert await iam_service.get_effective_permissions(db, subject) == set()
