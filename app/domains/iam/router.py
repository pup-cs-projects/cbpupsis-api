"""HTTP layer for the IAM domain: manage permissions, policies, and groups.

These endpoints are the AWS-IAM-style management surface: create the building
blocks (permissions), bundle them into policies, create groups, attach policies
to groups, and add users to groups. They are themselves permission-protected—
managing IAM requires the ``ManageIAM`` permission—so only suitably privileged
principals can reshape authorization.

Endpoints stay thin; all logic lives in :mod:`app.domains.iam.service`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.domains.auth.dependencies import CurrentUser
from app.domains.iam import service as iam_service
from app.domains.iam.constants import MANAGE_IAM
from app.domains.iam.dependencies import require_admin_permission
from app.domains.iam.schemas import (
    AddUserToGroupRequest,
    AttachPolicyRequest,
    EffectivePermissionsRead,
    GroupCreate,
    GroupRead,
    PermissionCreate,
    PermissionRead,
    PolicyCreate,
    PolicyRead,
)
from app.shared.pagination import Page

router = APIRouter()

# Every management endpoint requires this permission. Declared once and reused.
_manage = Depends(require_admin_permission(MANAGE_IAM))


@router.post(
    "/permissions",
    response_model=PermissionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_permission(
    data: PermissionCreate,
    user: CurrentUser = _manage,
    db: AsyncSession = Depends(get_db),
) -> PermissionRead:
    """Create a new permission (an atomic action)."""
    permission = await iam_service.create_permission(
        db, action=data.action, description=data.description, actor_id=user.id
    )
    return PermissionRead.model_validate(permission)


@router.post(
    "/policies",
    response_model=PolicyRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_policy(
    data: PolicyCreate,
    user: CurrentUser = _manage,
    db: AsyncSession = Depends(get_db),
) -> PolicyRead:
    """Create a policy bundling the named (existing) permissions."""
    policy = await iam_service.create_policy(
        db,
        name=data.name,
        permission_actions=data.permission_actions,
        description=data.description,
        actor_id=user.id,
    )
    return PolicyRead.model_validate(policy)


@router.post(
    "/groups",
    response_model=GroupRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_group(
    data: GroupCreate,
    user: CurrentUser = _manage,
    db: AsyncSession = Depends(get_db),
) -> GroupRead:
    """Create a new (empty) group."""
    group = await iam_service.create_group(
        db, name=data.name, description=data.description, actor_id=user.id
    )
    return GroupRead.model_validate(group)


@router.post(
    "/groups/{group_id}/policies",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def attach_policy_to_group(
    group_id: int,
    data: AttachPolicyRequest,
    user: CurrentUser = _manage,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Attach a policy to a group; members inherit its permissions."""
    await iam_service.attach_policy_to_group(
        db, group_id, data.policy_id, actor_id=user.id
    )


@router.post(
    "/groups/{group_id}/users",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def add_user_to_group(
    group_id: int,
    data: AddUserToGroupRequest,
    user: CurrentUser = _manage,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Add a user to a group, granting them the group's policies."""
    await iam_service.add_user_to_group(
        db, user_id=uuid.UUID(data.user_id), group_id=group_id, actor_id=user.id
    )


@router.delete(
    "/groups/{group_id}/policies/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def detach_policy_from_group(
    group_id: int,
    policy_id: int,
    user: CurrentUser = _manage,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Detach a policy from a group; members lose the permissions it granted.

    Takes effect immediately: permissions are resolved per request, never
    carried in the token.
    """
    await iam_service.detach_policy_from_group(
        db, group_id, policy_id, actor_id=user.id
    )


@router.delete(
    "/groups/{group_id}/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_user_from_group(
    group_id: int,
    user_id: uuid.UUID,
    user: CurrentUser = _manage,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a user from a group, revoking the policies it granted them."""
    await iam_service.remove_user_from_group(
        db, user_id=user_id, group_id=group_id, actor_id=user.id
    )


# --------------------------------------------------------------------------- #
# Read endpoints. The management API was write-only: it could reshape
# authorization but never show it, which leaves an admin screen unable to
# render the model it is editing.
# --------------------------------------------------------------------------- #


@router.get("/permissions", response_model=Page[PermissionRead], dependencies=[_manage])
async def list_permissions(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[PermissionRead]:
    """List every permission the application understands."""
    page = await iam_service.list_permissions(db, limit=limit, offset=offset)
    return Page[PermissionRead](
        items=[PermissionRead.model_validate(row) for row in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/permissions/{permission_id}",
    response_model=PermissionRead,
    dependencies=[_manage],
)
async def read_permission(
    permission_id: int, db: AsyncSession = Depends(get_db)
) -> PermissionRead:
    """Return one permission."""
    permission = await iam_service.get_permission(db, permission_id)
    return PermissionRead.model_validate(permission)


@router.get("/policies", response_model=Page[PolicyRead], dependencies=[_manage])
async def list_policies(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[PolicyRead]:
    """List policies, each with the permissions it grants."""
    page = await iam_service.list_policies(db, limit=limit, offset=offset)
    return Page[PolicyRead](
        items=[PolicyRead.model_validate(row) for row in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/policies/{policy_id}", response_model=PolicyRead, dependencies=[_manage])
async def read_policy(policy_id: int, db: AsyncSession = Depends(get_db)) -> PolicyRead:
    """Return one policy with its permissions."""
    policy = await iam_service.get_policy(db, policy_id)
    return PolicyRead.model_validate(policy)


@router.get("/groups", response_model=Page[GroupRead], dependencies=[_manage])
async def list_groups(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> Page[GroupRead]:
    """List groups."""
    page = await iam_service.list_groups(db, limit=limit, offset=offset)
    return Page[GroupRead](
        items=[GroupRead.model_validate(row) for row in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/groups/{group_id}", response_model=GroupRead, dependencies=[_manage])
async def read_group(group_id: int, db: AsyncSession = Depends(get_db)) -> GroupRead:
    """Return one group."""
    group = await iam_service.get_group(db, group_id)
    return GroupRead.model_validate(group)


@router.get(
    "/users/{user_id}/permissions",
    response_model=EffectivePermissionsRead,
    dependencies=[_manage],
)
async def read_user_permissions(
    user_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> EffectivePermissionsRead:
    """Return a user's effective permissions, split by how they were granted.

    The view an admin screen needs and that nothing exposed: the union is what
    the guards check, while the two sources are what an operator changes to
    revoke a grant.
    """
    effective, from_groups, from_direct = await iam_service.get_permission_breakdown(
        db, user_id
    )
    return EffectivePermissionsRead(
        user_id=str(user_id),
        effective=sorted(effective),
        from_groups=sorted(from_groups),
        from_direct=sorted(from_direct),
    )
