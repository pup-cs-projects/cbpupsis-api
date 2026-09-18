"""Business logic for the IAM domain.

This module is the IAM domain's public interface. It is framework-agnostic (it
knows nothing about HTTP) so it can be called from routers, background jobs, or
tests alike. Other domains and the auth layer should call these functions
rather than importing IAM models or querying IAM tables directly.

No SQL lives here: the queries are in ``repository.py``. This module owns the
rules layered on them — that grants are additive, that a policy may not be
created against a permission that does not exist, and that re-attaching an
existing edge is a no-op rather than an error.

The centrepiece is :func:`get_effective_permissions`, which resolves the full
set of actions a user is allowed to perform by walking the
user -> group -> policy -> permission graph (plus any directly attached
policies) and unioning the results.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event, event_bus
from app.domains.iam import repository
from app.domains.iam.exceptions import (
    GroupNotFoundError,
    PermissionNotFoundError,
    PolicyNotFoundError,
    UnknownPermissionsError,
)
from app.domains.iam.models import Group, Permission, Policy
from app.shared.pagination import Page


async def _audit(
    name: str, actor_id: uuid.UUID | None, payload: dict[str, object]
) -> None:
    """Emit an IAM change event for the audit trail.

    Emitted rather than written: this domain does not know that an audit domain
    exists, which is what stops "record the change" from becoming a step every
    future IAM operation must remember. It is also why the audit row is not part
    of this transaction — see ``app.domains.audit.service`` for what that costs.

    Published after the commit, deliberately. An event announcing a change that
    a later rollback undid is a trail describing something that never happened.
    """
    await event_bus.publish(
        Event(
            name=name,
            payload={**payload, "actor_id": str(actor_id) if actor_id else None},
        )
    )


async def get_effective_permissions(db: AsyncSession, user_id: uuid.UUID) -> set[str]:
    """Return the set of action names a user is effectively granted.

    Resolution combines two sources and unions their permissions:

    1. Group path: user -> ``user_groups`` -> ``group_policies`` ->
       ``policy_permissions`` -> permission.
    2. Direct path: user -> ``user_policies`` -> ``policy_permissions`` ->
       permission.

    The result is a plain ``set[str]`` of action names (e.g.
    ``{"ReadItem", "CreateItem"}``), which is cheap to cache and simple for
    callers to test membership against.

    Notes
    -----
    Resolution runs as two queries, one per grant path, unioned here. For hot paths,
    cache the returned set per user (e.g. in Redis) and invalidate on any
    membership or policy change. The database—not the auth token—must remain the
    source of truth so permission changes take effect immediately.
    """
    from_groups = await repository.list_group_permission_actions_for_user(db, user_id)
    from_direct = await repository.list_direct_permission_actions_for_user(db, user_id)

    # Union both sources; a set naturally de-duplicates overlapping grants.
    # The union is the business rule -- grants are additive, and there is no
    # explicit deny -- so it stays here rather than being folded into a query.
    return set(from_groups) | set(from_direct)


# --------------------------------------------------------------------------- #
# Management operations (create the building blocks; wire them together).
# --------------------------------------------------------------------------- #
async def create_permission(
    db: AsyncSession,
    action: str,
    description: str | None = None,
    actor_id: uuid.UUID | None = None,
) -> Permission:
    """Create a new permission (an atomic action).

    ``actor_id`` is recorded in the audit trail. Optional because seeds and
    scripts legitimately have no human behind them, and inventing one would put
    a falsehood in the one table that must not contain any.
    """
    permission = repository.add_permission(db, action=action, description=description)
    await db.commit()
    await db.refresh(permission)
    await _audit(
        "iam.permission_created",
        actor_id,
        {"permission_id": permission.id, "action": action},
    )
    return permission


async def create_policy(
    db: AsyncSession,
    name: str,
    permission_actions: list[str],
    description: str | None = None,
    actor_id: uuid.UUID | None = None,
) -> Policy:
    """Create a policy and attach the named permissions to it.

    ``permission_actions`` must reference permissions that already exist;
    a :class:`NotFoundError` is raised if any is missing, so a policy is never
    created with a partially resolved permission set.
    """
    # Resolve action names to Permission rows up front so we fail cleanly.
    permissions = await repository.list_permissions_by_actions(db, permission_actions)

    found_actions = {p.action for p in permissions}
    missing = set(permission_actions) - found_actions
    if missing:
        raise UnknownPermissionsError(missing)

    policy = await repository.add_policy(db, name=name, description=description)

    for permission in permissions:
        repository.add_policy_permission(db, policy.id, permission.id)

    await db.commit()
    await db.refresh(policy)
    await _audit(
        "iam.policy_created",
        actor_id,
        {
            "policy_id": policy.id,
            "name": name,
            "permission_actions": sorted(permission_actions),
        },
    )
    return policy


async def create_group(
    db: AsyncSession,
    name: str,
    description: str | None = None,
    actor_id: uuid.UUID | None = None,
) -> Group:
    """Create a new (empty) group."""
    group = repository.add_group(db, name=name, description=description)
    await db.commit()
    await db.refresh(group)
    await _audit("iam.group_created", actor_id, {"group_id": group.id, "name": name})
    return group


async def attach_policy_to_group(
    db: AsyncSession, group_id: int, policy_id: int, actor_id: uuid.UUID | None = None
) -> None:
    """Attach a policy to a group so members inherit its permissions.

    Idempotent, like the other attachment helpers. The audit entry is written
    only when something actually changed — recording a no-op as a grant would
    make the trail describe events that never happened.
    """
    if await repository.exists_group_policy(db, group_id, policy_id):
        return
    repository.add_group_policy(db, group_id, policy_id)
    await db.commit()
    await _audit(
        "iam.policy_attached_to_group",
        actor_id,
        {"group_id": group_id, "policy_id": policy_id},
    )


async def add_user_to_group(
    db: AsyncSession,
    user_id: uuid.UUID,
    group_id: int,
    actor_id: uuid.UUID | None = None,
) -> None:
    """Add a user to a group, granting them the group's policies.

    Idempotent: re-adding an existing member is a no-op rather than an
    integrity error, so callers need not check membership first.
    """
    if await repository.exists_user_group(db, user_id, group_id):
        return
    repository.add_user_group(db, user_id, group_id)
    await db.commit()
    await _audit(
        "iam.user_added_to_group",
        actor_id,
        {"user_id": str(user_id), "group_id": group_id},
    )


async def remove_user_from_group(
    db: AsyncSession,
    user_id: uuid.UUID,
    group_id: int,
    actor_id: uuid.UUID | None = None,
) -> None:
    """Remove a user from a group. Takes effect on their next request."""
    await repository.delete_user_group(db, user_id, group_id)
    await db.commit()
    await _audit(
        "iam.user_removed_from_group",
        actor_id,
        {"user_id": str(user_id), "group_id": group_id},
    )


async def attach_policy_to_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    policy_id: int,
    actor_id: uuid.UUID | None = None,
) -> None:
    """Attach a policy directly to a user, bypassing groups.

    Use sparingly — group membership is easier to audit — but occasionally a
    one-off grant does not justify a dedicated group.
    """
    if await repository.exists_user_policy(db, user_id, policy_id):
        return
    repository.add_user_policy(db, user_id, policy_id)
    await db.commit()
    await _audit(
        "iam.policy_attached_to_user",
        actor_id,
        {"user_id": str(user_id), "policy_id": policy_id},
    )


async def detach_policy_from_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    policy_id: int,
    actor_id: uuid.UUID | None = None,
) -> None:
    """Remove a directly attached policy from a user."""
    await repository.delete_user_policy(db, user_id, policy_id)
    await db.commit()
    await _audit(
        "iam.policy_detached_from_user",
        actor_id,
        {"user_id": str(user_id), "policy_id": policy_id},
    )


# --------------------------------------------------------------------------- #
# Read operations (what an admin screen needs to render the model).
# --------------------------------------------------------------------------- #
async def list_permissions(
    db: AsyncSession, limit: int = 50, offset: int = 0
) -> Page[Permission]:
    """Return a page of every permission the application understands."""
    rows, total = await repository.list_permissions(db, limit=limit, offset=offset)
    return Page(items=rows, total=total, limit=limit, offset=offset)


async def get_permission(db: AsyncSession, permission_id: int) -> Permission:
    """Return one permission, or raise :class:`PermissionNotFoundError`."""
    permission = await repository.get_permission(db, permission_id)
    if permission is None:
        raise PermissionNotFoundError(permission_id)
    return permission


async def list_policies(
    db: AsyncSession, limit: int = 50, offset: int = 0
) -> Page[Policy]:
    """Return a page of policies, each with the permissions it grants."""
    rows, total = await repository.list_policies(db, limit=limit, offset=offset)
    return Page(items=rows, total=total, limit=limit, offset=offset)


async def get_policy(db: AsyncSession, policy_id: int) -> Policy:
    """Return one policy with its permissions, or raise
    :class:`PolicyNotFoundError`."""
    policy = await repository.get_policy(db, policy_id)
    if policy is None:
        raise PolicyNotFoundError(policy_id)
    return policy


async def list_groups(
    db: AsyncSession, limit: int = 50, offset: int = 0
) -> Page[Group]:
    """Return a page of groups."""
    rows, total = await repository.list_groups(db, limit=limit, offset=offset)
    return Page(items=rows, total=total, limit=limit, offset=offset)


async def get_group(db: AsyncSession, group_id: int) -> Group:
    """Return one group, or raise :class:`GroupNotFoundError`."""
    group = await repository.get_group(db, group_id)
    if group is None:
        raise GroupNotFoundError(group_id)
    return group


async def detach_policy_from_group(
    db: AsyncSession, group_id: int, policy_id: int, actor_id: uuid.UUID | None = None
) -> None:
    """Detach a policy from a group; members lose the permissions it granted.

    The counterpart :func:`attach_policy_to_group` shipped without. Idempotent
    in the same way as the other detach helpers: removing an edge that is not
    there is a no-op, not an error.

    Takes effect on the members' next request — permissions are resolved from
    the database per request, never carried in a token, which is what makes a
    revocation immediate rather than pending until a token expires.
    """
    await repository.delete_group_policy(db, group_id, policy_id)
    await db.commit()
    await _audit(
        "iam.policy_detached_from_group",
        actor_id,
        {"group_id": group_id, "policy_id": policy_id},
    )


async def get_permission_breakdown(
    db: AsyncSession, user_id: uuid.UUID
) -> tuple[set[str], set[str], set[str]]:
    """Return ``(effective, from_groups, from_direct)`` for one user.

    The same two queries as :func:`get_effective_permissions`, kept apart
    instead of unioned. This is why the repository exposes the two paths as
    separate functions: "which permissions does this user get from their
    groups?" is the question an admin answering "why can they do that?" needs,
    and the merged set cannot answer it.

    Not folded into :func:`get_effective_permissions` — that one is called on
    every authorized request and its callers want a single membership test, so
    it stays the narrow, hot path.
    """
    from_groups = set(
        await repository.list_group_permission_actions_for_user(db, user_id)
    )
    from_direct = set(
        await repository.list_direct_permission_actions_for_user(db, user_id)
    )
    return from_groups | from_direct, from_groups, from_direct
