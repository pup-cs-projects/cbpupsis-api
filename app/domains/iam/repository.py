"""Data access for the IAM domain: the permission/policy/group graph.

The interesting functions here are the two halves of permission resolution.
:func:`list_group_permission_actions_for_user` and
:func:`list_direct_permission_actions_for_user` each walk one path through the
graph and return a plain list of action names. They are deliberately **two
functions returning two lists**, not one that unions them: the union is a rule
about how grants combine, and rules live in the service.

Keeping them separate also keeps them individually meaningful — "which
permissions does this user get from their groups?" is exactly the question an
admin screen or an audit needs answered, and it is unavailable if the only
entry point is the merged set.

The attachment helpers (``add_*``/``delete_*``) are paired with ``exists_*``
lookups rather than doing their own idempotency check, so the service can decide
what a duplicate means. Here that means "no-op"; elsewhere it might mean a 409.

Transactions belong to the service: nothing here commits. ``flush()`` is used
where a server-generated id is needed before the transaction ends.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.iam.models import (
    Group,
    GroupPolicy,
    Permission,
    Policy,
    PolicyPermission,
    UserGroup,
    UserPolicy,
)

# --------------------------------------------------------------------------- #
# Permission resolution
# --------------------------------------------------------------------------- #


async def list_group_permission_actions_for_user(
    db: AsyncSession, user_id: uuid.UUID
) -> list[str]:
    """Return action names granted via group membership.

    Walks user -> ``user_groups`` -> ``group_policies`` -> ``policy_permissions``
    -> permission in one query. May contain duplicates when two of the user's
    groups grant the same action; de-duplication is the caller's concern,
    because the caller is already building a set out of this and the other path.

    SQL::

        SELECT permissions.action
        FROM user_groups
        JOIN group_policies
          ON group_policies.group_id = user_groups.group_id
        JOIN policy_permissions
          ON policy_permissions.policy_id = group_policies.policy_id
        JOIN permissions
          ON permissions.id = policy_permissions.permission_id
        WHERE user_groups.user_id = :user_id_1::UUID
    """
    result = await db.execute(
        select(Permission.action)
        .select_from(UserGroup)
        .join(GroupPolicy, GroupPolicy.group_id == UserGroup.group_id)
        .join(PolicyPermission, PolicyPermission.policy_id == GroupPolicy.policy_id)
        .join(Permission, Permission.id == PolicyPermission.permission_id)
        .where(UserGroup.user_id == user_id)
    )
    return list(result.scalars().all())


async def list_direct_permission_actions_for_user(
    db: AsyncSession, user_id: uuid.UUID
) -> list[str]:
    """Return action names granted by policies attached directly to the user.

    The second, group-bypassing path: user -> ``user_policies`` ->
    ``policy_permissions`` -> permission.

    SQL::

        SELECT permissions.action
        FROM user_policies
        JOIN policy_permissions
          ON policy_permissions.policy_id = user_policies.policy_id
        JOIN permissions
          ON permissions.id = policy_permissions.permission_id
        WHERE user_policies.user_id = :user_id_1::UUID
    """
    result = await db.execute(
        select(Permission.action)
        .select_from(UserPolicy)
        .join(PolicyPermission, PolicyPermission.policy_id == UserPolicy.policy_id)
        .join(Permission, Permission.id == PolicyPermission.permission_id)
        .where(UserPolicy.user_id == user_id)
    )
    return list(result.scalars().all())


# --------------------------------------------------------------------------- #
# Permissions, policies, groups
# --------------------------------------------------------------------------- #


async def list_permissions_by_actions(
    db: AsyncSession, actions: Sequence[str]
) -> list[Permission]:
    """Return the permission rows matching any of ``actions``.

    Returns only what exists. Working out that the result is *smaller* than the
    input — and refusing to build a half-resolved policy because of it — is the
    service's rule, not this layer's.

    The ``IN`` list is an expanding parameter, rendered by the driver as one
    placeholder per element of ``actions`` at execution time.

    SQL::

        SELECT permissions.id, permissions.action, permissions.description,
               permissions.created_at, permissions.updated_at
        FROM permissions
        WHERE permissions.action IN (__[POSTCOMPILE_action_1])
    """
    result = await db.execute(select(Permission).where(Permission.action.in_(actions)))
    return list(result.scalars().all())


def add_permission(
    db: AsyncSession, action: str, description: str | None = None
) -> Permission:
    """Stage a new permission on the session and return it.

    Emits no SQL here. On the service's commit::

        INSERT INTO permissions (action, description)
        VALUES (:action, :description)
        RETURNING permissions.id, permissions.created_at,
                  permissions.updated_at
    """
    permission = Permission(action=action, description=description)
    db.add(permission)
    return permission


async def add_policy(
    db: AsyncSession, name: str, description: str | None = None
) -> Policy:
    """Stage a new policy, flush to assign its id, and return it.

    Flushes rather than commits: the caller still has permission rows to attach,
    and those need ``policy.id``. Ending the transaction here would leave a
    policy with no permissions visible to other sessions if the attach failed.

    Unlike the other ``add_*`` helpers this one does emit its INSERT here, at
    the ``flush()``, rather than deferring it to the service's commit.

    SQL::

        INSERT INTO policies (name, description)
        VALUES (:name, :description)
        RETURNING policies.id, policies.created_at, policies.updated_at
    """
    policy = Policy(name=name, description=description)
    db.add(policy)
    await db.flush()  # assign policy.id without ending the transaction
    return policy


def add_group(db: AsyncSession, name: str, description: str | None = None) -> Group:
    """Stage a new (empty) group on the session and return it.

    Emits no SQL here. On the service's commit::

        INSERT INTO groups (name, description)
        VALUES (:name, :description)
        RETURNING groups.id, groups.created_at, groups.updated_at
    """
    group = Group(name=name, description=description)
    db.add(group)
    return group


# --------------------------------------------------------------------------- #
# Attachments (the edges of the graph)
# --------------------------------------------------------------------------- #


def add_policy_permission(
    db: AsyncSession, policy_id: int, permission_id: int
) -> PolicyPermission:
    """Stage a policy -> permission edge and return it.

    Emits no SQL here. On the service's commit::

        INSERT INTO policy_permissions (policy_id, permission_id)
        VALUES (:policy_id, :permission_id)
    """
    edge = PolicyPermission(policy_id=policy_id, permission_id=permission_id)
    db.add(edge)
    return edge


async def exists_group_policy(db: AsyncSession, group_id: int, policy_id: int) -> bool:
    """Return whether a policy is already attached to a group.

    SQL::

        SELECT group_policies.group_id, group_policies.policy_id
        FROM group_policies
        WHERE group_policies.group_id = :group_id_1
          AND group_policies.policy_id = :policy_id_1
    """
    edge = await db.scalar(
        select(GroupPolicy).where(
            GroupPolicy.group_id == group_id, GroupPolicy.policy_id == policy_id
        )
    )
    return edge is not None


def add_group_policy(db: AsyncSession, group_id: int, policy_id: int) -> GroupPolicy:
    """Stage a group -> policy edge and return it.

    Emits no SQL here. On the service's commit::

        INSERT INTO group_policies (group_id, policy_id)
        VALUES (:group_id, :policy_id)
    """
    edge = GroupPolicy(group_id=group_id, policy_id=policy_id)
    db.add(edge)
    return edge


async def exists_user_group(
    db: AsyncSession, user_id: uuid.UUID, group_id: int
) -> bool:
    """Return whether a user is already a member of a group.

    SQL::

        SELECT user_groups.user_id, user_groups.group_id
        FROM user_groups
        WHERE user_groups.user_id = :user_id_1::UUID
          AND user_groups.group_id = :group_id_1
    """
    edge = await db.scalar(
        select(UserGroup).where(
            UserGroup.user_id == user_id, UserGroup.group_id == group_id
        )
    )
    return edge is not None


async def is_user_in_group_named(
    db: AsyncSession, user_id: uuid.UUID, group_name: str
) -> bool:
    """Return whether ``user_id`` belongs to the group named ``group_name``.

    SQL::

        SELECT user_groups.user_id
        FROM user_groups
        JOIN groups ON groups.id = user_groups.group_id
        WHERE user_groups.user_id = :user_id_1::UUID
          AND groups.name = :name_1
        LIMIT 1
    """
    result = await db.scalar(
        select(UserGroup.user_id)
        .join(Group, Group.id == UserGroup.group_id)
        .where(UserGroup.user_id == user_id, Group.name == group_name)
        .limit(1)
    )
    return result is not None


def add_user_group(db: AsyncSession, user_id: uuid.UUID, group_id: int) -> UserGroup:
    """Stage a user -> group membership edge and return it.

    Emits no SQL here. On the service's commit::

        INSERT INTO user_groups (user_id, group_id)
        VALUES (:user_id::UUID, :group_id)
    """
    edge = UserGroup(user_id=user_id, group_id=group_id)
    db.add(edge)
    return edge


async def delete_user_group(
    db: AsyncSession, user_id: uuid.UUID, group_id: int
) -> None:
    """Remove a user's membership of a group. A no-op if it was not there.

    A set-based DELETE rather than load-then-remove: one round trip, and no
    error to handle when the row is already gone, which is what makes the
    service's remove operation naturally idempotent.

    SQL::

        DELETE FROM user_groups
        WHERE user_groups.user_id = :user_id_1::UUID
          AND user_groups.group_id = :group_id_1
    """
    await db.execute(
        delete(UserGroup).where(
            UserGroup.user_id == user_id, UserGroup.group_id == group_id
        )
    )


async def exists_user_policy(
    db: AsyncSession, user_id: uuid.UUID, policy_id: int
) -> bool:
    """Return whether a policy is already attached directly to a user.

    SQL::

        SELECT user_policies.user_id, user_policies.policy_id
        FROM user_policies
        WHERE user_policies.user_id = :user_id_1::UUID
          AND user_policies.policy_id = :policy_id_1
    """
    edge = await db.scalar(
        select(UserPolicy).where(
            UserPolicy.user_id == user_id, UserPolicy.policy_id == policy_id
        )
    )
    return edge is not None


def add_user_policy(db: AsyncSession, user_id: uuid.UUID, policy_id: int) -> UserPolicy:
    """Stage a direct user -> policy edge and return it.

    Emits no SQL here. On the service's commit::

        INSERT INTO user_policies (user_id, policy_id)
        VALUES (:user_id::UUID, :policy_id)
    """
    edge = UserPolicy(user_id=user_id, policy_id=policy_id)
    db.add(edge)
    return edge


async def delete_user_policy(
    db: AsyncSession, user_id: uuid.UUID, policy_id: int
) -> None:
    """Remove a directly attached policy from a user. A no-op if absent.

    SQL::

        DELETE FROM user_policies
        WHERE user_policies.user_id = :user_id_1::UUID
          AND user_policies.policy_id = :policy_id_1
    """
    await db.execute(
        delete(UserPolicy).where(
            UserPolicy.user_id == user_id, UserPolicy.policy_id == policy_id
        )
    )


# --------------------------------------------------------------------------- #
# Listing and lookup (the read half of the management API)
# --------------------------------------------------------------------------- #


async def list_permissions(
    db: AsyncSession, limit: int, offset: int
) -> tuple[list[Permission], int]:
    """Return one page of permissions and the total count.

    Ordered by ``action`` rather than by id: the action is what an admin screen
    shows and what a reader scans for, and it is unique, so the ordering is
    total and pages cannot repeat or skip a row.

    Two statements, like every paginated read here — the count comes from the
    same query before limit and offset, so it describes the whole set.

    SQL::

        -- 1. total matching rows, before limit/offset
        SELECT count(*) AS count_1
        FROM (SELECT permissions.id AS id, permissions.action AS action,
                     permissions.description AS description,
                     permissions.created_at AS created_at,
                     permissions.updated_at AS updated_at
              FROM permissions) AS anon_1

        -- 2. the page itself
        SELECT permissions.id, permissions.action, permissions.description,
               permissions.created_at, permissions.updated_at
        FROM permissions
        ORDER BY permissions.action
        LIMIT :param_1 OFFSET :param_2
    """
    query = select(Permission)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    result = await db.execute(
        query.order_by(Permission.action).limit(limit).offset(offset)
    )
    return list(result.scalars().all()), total or 0


async def get_permission(db: AsyncSession, permission_id: int) -> Permission | None:
    """Return one permission by id, or ``None``.

    SQL::

        SELECT permissions.id, permissions.action, permissions.description,
               permissions.created_at, permissions.updated_at
        FROM permissions
        WHERE permissions.id = :id_1
    """
    return await db.scalar(select(Permission).where(Permission.id == permission_id))


async def list_policies(
    db: AsyncSession, limit: int, offset: int
) -> tuple[list[Policy], int]:
    """Return one page of policies and the total count.

    Each policy carries its permissions: ``Policy.permissions`` is
    ``lazy="selectin"``, so they arrive in one extra query for the whole page
    rather than one per row, and without the row multiplication a JOIN would
    cause. The count query below is unaffected by it.

    SQL::

        -- 1. total matching rows, before limit/offset
        SELECT count(*) AS count_1
        FROM (SELECT policies.id AS id, policies.name AS name,
                     policies.description AS description,
                     policies.created_at AS created_at,
                     policies.updated_at AS updated_at
              FROM policies) AS anon_1

        -- 2. the page itself
        SELECT policies.id, policies.name, policies.description,
               policies.created_at, policies.updated_at
        FROM policies
        ORDER BY policies.name
        LIMIT :param_1 OFFSET :param_2

        -- 3. the selectin load of each policy's permissions
        SELECT policy_permissions.policy_id AS policy_permissions_policy_id,
               permissions.id, permissions.action, permissions.description,
               permissions.created_at, permissions.updated_at
        FROM policy_permissions
        JOIN permissions ON permissions.id = policy_permissions.permission_id
        WHERE policy_permissions.policy_id IN (__[POSTCOMPILE_primary_keys])
    """
    query = select(Policy)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    result = await db.execute(query.order_by(Policy.name).limit(limit).offset(offset))
    return list(result.scalars().all()), total or 0


async def get_policy(db: AsyncSession, policy_id: int) -> Policy | None:
    """Return one policy by id, with its permissions, or ``None``.

    SQL::

        SELECT policies.id, policies.name, policies.description,
               policies.created_at, policies.updated_at
        FROM policies
        WHERE policies.id = :id_1

        -- then the selectin load of its permissions
        SELECT policy_permissions.policy_id AS policy_permissions_policy_id,
               permissions.id, permissions.action, permissions.description,
               permissions.created_at, permissions.updated_at
        FROM policy_permissions
        JOIN permissions ON permissions.id = policy_permissions.permission_id
        WHERE policy_permissions.policy_id IN (__[POSTCOMPILE_primary_keys])
    """
    return await db.scalar(select(Policy).where(Policy.id == policy_id))


async def list_groups(
    db: AsyncSession, limit: int, offset: int
) -> tuple[list[Group], int]:
    """Return one page of groups and the total count.

    SQL::

        -- 1. total matching rows, before limit/offset
        SELECT count(*) AS count_1
        FROM (SELECT groups.id AS id, groups.name AS name,
                     groups.description AS description,
                     groups.created_at AS created_at,
                     groups.updated_at AS updated_at
              FROM groups) AS anon_1

        -- 2. the page itself
        SELECT groups.id, groups.name, groups.description, groups.created_at,
               groups.updated_at
        FROM groups
        ORDER BY groups.name
        LIMIT :param_1 OFFSET :param_2
    """
    query = select(Group)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    result = await db.execute(query.order_by(Group.name).limit(limit).offset(offset))
    return list(result.scalars().all()), total or 0


async def get_group(db: AsyncSession, group_id: int) -> Group | None:
    """Return one group by id, or ``None``.

    SQL::

        SELECT groups.id, groups.name, groups.description, groups.created_at,
               groups.updated_at
        FROM groups
        WHERE groups.id = :id_1
    """
    return await db.scalar(select(Group).where(Group.id == group_id))


async def delete_group_policy(db: AsyncSession, group_id: int, policy_id: int) -> None:
    """Detach a policy from a group. A no-op if it was not attached.

    The counterpart :func:`add_group_policy` never had. A set-based DELETE for
    the same reason as :func:`delete_user_group`: one round trip, and nothing to
    handle when the edge is already gone.

    SQL::

        DELETE FROM group_policies
        WHERE group_policies.group_id = :group_id_1
          AND group_policies.policy_id = :policy_id_1
    """
    await db.execute(
        delete(GroupPolicy).where(
            GroupPolicy.group_id == group_id, GroupPolicy.policy_id == policy_id
        )
    )
