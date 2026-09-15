"""HTTP layer for the users domain.

Thin by design: endpoints parse the request, delegate to the service, and shape
the response.

Note the two kinds of protection in use here, which are not interchangeable:

- ``/me`` needs only authentication — the user id comes from the token, so
  there is no way to address someone else's record.
- ``/{user_id}`` needs authorization, because the id is caller-supplied. It
  demands the ``ReadAllUser`` permission *or* self-ownership; a permission check
  alone would let any authenticated user read every profile.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.domains.auth.dependencies import CurrentUser, get_current_user
from app.domains.iam.dependencies import require_permission
from app.domains.users import service as users_service
from app.domains.users.constants import MANAGE_USER, READ_ALL_USER
from app.domains.users.schemas import DeleteAccountRequest, UserRead, UserUpdate
from app.shared.pagination import Page

router = APIRouter()


@router.get("/me", response_model=UserRead)
async def read_me(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserRead:
    """Return the authenticated user's own profile."""
    record = await users_service.get_by_id(db, user.id)
    return UserRead.model_validate(record)


@router.patch("/me", response_model=UserRead)
async def update_me(
    data: UserUpdate,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserRead:
    """Update the authenticated user's own profile.

    A true partial update: ``exclude_unset=True`` passes on only the fields the
    client actually sent, so omitting one leaves it unchanged while sending it
    as ``null`` clears it. Dumping the whole model instead would blank every
    field the client did not mention.
    """
    record = await users_service.update_profile(
        db, user.id, **data.model_dump(exclude_unset=True)
    )
    return UserRead.model_validate(record)


@router.post("/me/complete-onboarding", response_model=UserRead)
async def complete_onboarding(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserRead:
    """Mark the authenticated user's onboarding as finished.

    422 listing the missing fields if the required profile data is not yet set.
    Idempotent: calling it again keeps the original completion timestamp.
    """
    record = await users_service.complete_onboarding(db, user.id)
    return UserRead.model_validate(record)


@router.post("/me/deactivate", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_me(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Deactivate the authenticated user's own account.

    Reversible: nothing is erased, and an operator can reactivate the account.
    All refresh tokens are revoked, so every session ends immediately.
    """
    await users_service.deactivate_own_account(db, user.id)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(
    data: DeleteAccountRequest,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete the authenticated user's own account.

    Requires the account password — a stolen access token must not be enough to
    delete an account. The row survives so records referencing it are not
    orphaned, but every identifying field is scrubbed and all sessions revoked.
    401 if the password is wrong.
    """
    await users_service.delete_own_account(db, user.id, data.password)


@router.get("", response_model=Page[UserRead])
async def list_users(
    is_active: bool | None = Query(
        default=None, description="Filter by account status. Omit for both."
    ),
    is_verified: bool | None = Query(
        default=None, description="Filter by email verification. Omit for both."
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(require_permission(READ_ALL_USER)),
    db: AsyncSession = Depends(get_db),
) -> Page[UserRead]:
    """List user accounts, newest first. Requires ``ReadAllUser``.

    Both filters are tri-state: omitting one returns accounts in either state,
    which is what makes "everyone" and "only the unverified" the same endpoint.
    """
    page = await users_service.list_users_for(
        db,
        viewer_id=user.id,
        limit=limit,
        offset=offset,
        is_active=is_active,
        is_verified=is_verified,
    )
    return Page[UserRead](
        items=[UserRead.model_validate(record) for record in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post("/{user_id}/deactivate", response_model=UserRead)
async def deactivate_user(
    user_id: uuid.UUID,
    user: CurrentUser = Depends(require_permission(MANAGE_USER)),
    db: AsyncSession = Depends(get_db),
) -> UserRead:
    """Suspend another user's account. Requires ``ManageUser``.

    Reversible, and every session is revoked immediately. Deliberately not a
    DELETE: account deletion stays self-service and password-confirmed, because
    an administrator cannot supply the password that authorizes it.

    422 if the caller aims it at themselves — see the service for why.
    """
    record = await users_service.deactivate_as_admin(
        db, actor_id=user.id, user_id=user_id
    )
    return UserRead.model_validate(record)


@router.post("/{user_id}/reactivate", response_model=UserRead)
async def reactivate_user(
    user_id: uuid.UUID,
    user: CurrentUser = Depends(require_permission(MANAGE_USER)),
    db: AsyncSession = Depends(get_db),
) -> UserRead:
    """Restore a suspended account. Requires ``ManageUser``.

    422 on a deleted account: deletion scrubs the identity, so there is nothing
    left to restore.
    """
    record = await users_service.reactivate_as_admin(
        db, actor_id=user.id, user_id=user_id
    )
    return UserRead.model_validate(record)


@router.get("/{user_id}", response_model=UserRead)
async def read_user(
    user_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserRead:
    """Return another user's profile.

    Allowed when the caller holds ``ReadAllUser`` or is asking about themselves.
    This object-level check cannot be expressed by ``require_permission`` alone,
    which answers only "may they read users in general?" — see
    ``app/domains/items/service.py`` for the same pattern on owned resources.
    """
    record = await users_service.get_profile_for(db, viewer_id=user.id, user_id=user_id)
    return UserRead.model_validate(record)
