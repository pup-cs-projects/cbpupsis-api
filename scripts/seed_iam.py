"""Seed the IAM tables with a baseline set of permissions, policies, and groups.

This script encodes the *initial* authorization model as reproducible data. It
is idempotent: it inserts rows only when they are missing, so it is safe to run
repeatedly (e.g. on deploy, or after adding a new permission below).

Because permissions and policies are data, evolving "who can do what" is often
just an edit here plus a re-run—no application code changes. The permission set
is application-specific; the ``*Item`` actions below match this template's
reference domain—replace them with the actions your application authorizes.

Run with::

    uv run python -m scripts.seed_iam
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from cbpupsis_database.models.iam import (
    Group,
    GroupPolicy,
    Permission,
    Policy,
    PolicyPermission,
)
from cbpupsis_database.session import AsyncSessionLocal
from cbpupsis_shared.domains.iam.constants import ADMIN_GROUP, SUPERADMIN_GROUP

# --------------------------------------------------------------------------- #
# Declarative seed data. Edit these three structures to change the baseline.
#
# Permissions and policies follow a FIXED vocabulary, so two developers naming
# the same capability land on the same string. Only GROUPS carries this
# project's own words.
#
#   Permission   Create<X> Read<X> Update<X> Delete<X>   -- own records only
#                ReadAll<X> Moderate<X> Manage<X>        -- crosses ownership
#   Policy       <X>Reader <X>Author <X>Moderator <X>Admin
#
# Never invent a synonym (View/Get/List/Edit/Modify/Remove) -- a codebase with
# both ReadItem and ViewItem has a 403 in it that no test will catch. Real state
# transitions (ApproveRefund, PublishListing) keep their business verb.
# See docs/tech-book/API-GUIDE.md (Authorization) for how this set is derived.
#
# The actions below stay LITERAL strings even though every one of them also has
# a constant in its domain's constants.py. That duplication is the point: this
# file is the independent declaration of what exists in the database, and
# tests/domains/iam/test_permission_constants.py checks the constants against
# it. Importing the constants here would make that test compare a value with
# itself, so a misspelled constant would seed its own typo and the check would
# pass while every guard using it denied all callers.
# ADMIN_GROUP is the deliberate exception: bootstrap_admin LOOKS UP the group
# this file CREATES, so there the two must be the same object, not two copies.
# --------------------------------------------------------------------------- #

#: All actions the application understands. (action, description)
PERMISSIONS: list[tuple[str, str]] = [
    # Ordinary verbs: always paired with an ownership check in the service.
    ("CreateItem", "Create an item"),
    ("ReadItem", "View your own items"),
    ("UpdateItem", "Update an item you own"),
    ("DeleteItem", "Delete an item you own"),
    # Elevated verbs: each one IS the grant that crosses ownership.
    ("ReadAllItem", "View items belonging to anyone"),
    ("ModerateItem", "Update or delete ANY item, regardless of owner"),
    ("ReadAllUser", "View any user's profile, not just your own"),
    ("ManageUser", "Deactivate or reactivate any user account"),
    ("ReadAllAuditEntry", "Read the administrative audit trail"),
    ("ReadAllNotification", "Read notifications belonging to anyone"),
    ("ManageNotificationDelivery", "Inspect and retry failed notification deliveries"),
    # Administering a resource itself, rather than its records.
    ("ManageIAM", "Manage permissions, policies, and groups"),
]

#: Policies as reusable bundles of the permissions above. name -> [actions]
POLICIES: dict[str, list[str]] = {
    "ItemAuthor": ["CreateItem", "ReadItem", "UpdateItem", "DeleteItem"],
    "ItemModerator": ["ReadAllItem", "ModerateItem"],
    "UserModerator": ["ReadAllUser"],
    "UserAdmin": ["ReadAllUser", "ManageUser"],
    "IAMAdmin": ["ManageIAM"],
    "AuditReader": ["ReadAllAuditEntry"],
    "NotificationModerator": ["ReadAllNotification"],
    "NotificationAdmin": ["ReadAllNotification", "ManageNotificationDelivery"],
}

#: Policies reserved for the separate Superadmin role. They are seeded now
#: because their handlers already exist, but attaching them to the Admin role
#: would cross the product boundary for issue #91.
RESERVED_POLICIES: frozenset[str] = frozenset(
    {"UserAdmin", "IAMAdmin", "AuditReader", "NotificationAdmin"}
)

#: Groups and the policies attached to each. name -> [policy names]
GROUPS: dict[str, list[str]] = {
    "Members": ["ItemAuthor"],
    "Moderators": [
        "ItemAuthor",
        "ItemModerator",
        "UserModerator",
        "NotificationModerator",
    ],
    # Admin is one authenticated role whose position controls data scope. Its
    # product permissions (Home, Calendar, Courses, Enrollment) will be attached
    # by those domains. User administration, IAM, and audit reading belong to
    # the separate Superadmin story and must not leak through this seed.
    ADMIN_GROUP: [],
    SUPERADMIN_GROUP: list(POLICIES),
}


async def _get_or_create_permission(
    session, action: str, description: str
) -> Permission:
    """Return the permission with ``action``, creating it if absent."""
    existing = await session.scalar(
        select(Permission).where(Permission.action == action)
    )
    if existing is not None:
        return existing
    permission = Permission(action=action, description=description)
    session.add(permission)
    await session.flush()
    return permission


async def _get_or_create_policy(session, name: str) -> Policy:
    """Return the policy named ``name``, creating it if absent."""
    existing = await session.scalar(select(Policy).where(Policy.name == name))
    if existing is not None:
        return existing
    policy = Policy(name=name)
    session.add(policy)
    await session.flush()
    return policy


async def _get_or_create_group(session, name: str) -> Group:
    """Return the group named ``name``, creating it if absent."""
    existing = await session.scalar(select(Group).where(Group.name == name))
    if existing is not None:
        return existing
    group = Group(name=name)
    session.add(group)
    await session.flush()
    return group


async def _ensure_policy_permission(
    session, policy_id: int, permission_id: int
) -> None:
    """Attach ``permission_id`` to ``policy_id`` if not already attached."""
    exists = await session.scalar(
        select(PolicyPermission).where(
            PolicyPermission.policy_id == policy_id,
            PolicyPermission.permission_id == permission_id,
        )
    )
    if exists is None:
        session.add(PolicyPermission(policy_id=policy_id, permission_id=permission_id))


async def _ensure_group_policy(session, group_id: int, policy_id: int) -> None:
    """Attach ``policy_id`` to ``group_id`` if not already attached."""
    exists = await session.scalar(
        select(GroupPolicy).where(
            GroupPolicy.group_id == group_id,
            GroupPolicy.policy_id == policy_id,
        )
    )
    if exists is None:
        session.add(GroupPolicy(group_id=group_id, policy_id=policy_id))


async def seed() -> None:
    """Create the baseline permissions, policies, and groups idempotently."""
    async with AsyncSessionLocal() as session:
        # 1) Permissions
        permissions: dict[str, Permission] = {}
        for action, description in PERMISSIONS:
            permissions[action] = await _get_or_create_permission(
                session, action, description
            )

        # 2) Policies, with their permissions attached
        policies: dict[str, Policy] = {}
        for policy_name, actions in POLICIES.items():
            policy = await _get_or_create_policy(session, policy_name)
            policies[policy_name] = policy
            for action in actions:
                await _ensure_policy_permission(
                    session, policy.id, permissions[action].id
                )

        # 3) Groups, with their policies attached
        for group_name, policy_names in GROUPS.items():
            group = await _get_or_create_group(session, group_name)
            for policy_name in policy_names:
                await _ensure_group_policy(session, group.id, policies[policy_name].id)

        await session.commit()
    print("IAM seed complete.")


if __name__ == "__main__":
    asyncio.run(seed())
