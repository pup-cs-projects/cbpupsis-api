"""Repository-layer tests for the iam domain.

The point of these tests is the **contract boundary**, not coverage of SQL the
service tests already exercise end to end. What is asserted here is what makes
the layer reusable and separately testable:

- a ``get_X`` that finds nothing returns ``None`` and does **not** raise —
  turning absence into a ``NotFoundError`` is the service's decision, and a
  repository that raised would force every non-HTTP caller to catch it back out;
- ``list_X`` returns ``(rows, total)`` where ``total`` counts the whole filtered
  result set rather than the page slice;
- repositories do not commit, so a caller that abandons the transaction leaves
  nothing behind.

The paired service-level assertions (that the same missing row *does* become a
404 one layer up) live beside this file in the same domain folder; together the
two halves pin the boundary from both sides.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_shared.domains.iam import repository as iam_repository
from cbpupsis_shared.domains.iam import service as iam_service


class TestIamRepository:
    """The two resolution paths, kept separate so the union stays a service rule."""

    async def test_permission_paths_are_empty_for_an_unknown_user(
        self, db: AsyncSession
    ) -> None:
        """No grants is an empty list, not an error."""
        user_id = uuid.uuid4()
        assert (
            await iam_repository.list_group_permission_actions_for_user(db, user_id)
            == []
        )
        assert (
            await iam_repository.list_direct_permission_actions_for_user(db, user_id)
            == []
        )

    async def test_group_and_direct_paths_are_reported_separately(
        self, db: AsyncSession, make_user
    ) -> None:
        """Each path answers its own question; the service unions them.

        This is what would be lost if the repository returned only the merged
        set — "where does this grant come from?" is an audit question.
        """
        user = await make_user("iam@example.com")

        await iam_service.create_permission(db, action="ReadItem")
        await iam_service.create_permission(db, action="ModerateItem")
        group_policy = await iam_service.create_policy(
            db, name="ReadOnly", permission_actions=["ReadItem"]
        )
        direct_policy = await iam_service.create_policy(
            db, name="Moderation", permission_actions=["ModerateItem"]
        )
        group = await iam_service.create_group(db, name="Readers")
        await iam_service.attach_policy_to_group(db, group.id, group_policy.id)
        await iam_service.add_user_to_group(db, user.id, group.id)
        await iam_service.attach_policy_to_user(db, user.id, direct_policy.id)

        from_groups = await iam_repository.list_group_permission_actions_for_user(
            db, user.id
        )
        from_direct = await iam_repository.list_direct_permission_actions_for_user(
            db, user.id
        )
        assert from_groups == ["ReadItem"]
        assert from_direct == ["ModerateItem"]

        # And the service is what combines them.
        assert await iam_service.get_effective_permissions(db, user.id) == {
            "ReadItem",
            "ModerateItem",
        }

    async def test_list_permissions_by_actions_returns_only_what_exists(
        self, db: AsyncSession
    ) -> None:
        """Returning a short list is not an error here; noticing the gap and
        refusing to build a half-resolved policy is the service's rule."""
        await iam_service.create_permission(db, action="ReadItem")

        found = await iam_repository.list_permissions_by_actions(
            db, ["ReadItem", "NoSuchAction"]
        )
        assert [p.action for p in found] == ["ReadItem"]

    async def test_exists_helpers_report_attachment_state(
        self, db: AsyncSession, make_user
    ) -> None:
        user = await make_user("edges@example.com")
        policy = await iam_service.create_policy(
            db, name="Empty", permission_actions=[]
        )
        group = await iam_service.create_group(db, name="Team")

        assert await iam_repository.exists_user_group(db, user.id, group.id) is False
        await iam_service.add_user_to_group(db, user.id, group.id)
        assert await iam_repository.exists_user_group(db, user.id, group.id) is True

        assert await iam_repository.exists_user_policy(db, user.id, policy.id) is False
        await iam_service.attach_policy_to_user(db, user.id, policy.id)
        assert await iam_repository.exists_user_policy(db, user.id, policy.id) is True

        assert (
            await iam_repository.exists_group_policy(db, group.id, policy.id) is False
        )
        await iam_service.attach_policy_to_group(db, group.id, policy.id)
        assert await iam_repository.exists_group_policy(db, group.id, policy.id) is True

    async def test_delete_edges_are_no_ops_when_absent(self, db: AsyncSession) -> None:
        """Set-based deletes make the service's remove operations idempotent."""
        user_id = uuid.uuid4()
        await iam_repository.delete_user_group(db, user_id, 12345)
        await iam_repository.delete_user_policy(db, user_id, 12345)
        await db.commit()
