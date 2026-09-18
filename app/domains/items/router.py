"""HTTP layer for the items domain — the reference router.

Demonstrates the full authn + authz chain: ``require_permission`` authenticates
the caller and checks a coarse permission, then the service applies the
object-level ownership check that the dependency cannot express. Both are
needed; see ``app.domains.items.service`` for why.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.domains.auth.dependencies import CurrentUser, get_current_user
from app.domains.iam.dependencies import require_permission
from app.domains.items import service as items_service
from app.domains.items.constants import CREATE_ITEM, DELETE_ITEM, UPDATE_ITEM
from app.domains.items.schemas import (
    ItemCreate,
    ItemRead,
    ItemReadDetail,
    ItemUpdate,
)
from app.shared.pagination import Page

router = APIRouter()


@router.get("", response_model=Page[ItemRead])
async def list_items(
    mine: bool = Query(
        default=False,
        description=(
            "Narrow the results to the caller's own items. Only meaningful to a "
            "caller holding ReadAllItem; everyone else is already scoped to "
            "their own."
        ),
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Page[ItemRead]:
    """List items the caller may see, newest first.

    Items are private: without ``ReadAllItem`` this returns only the caller's
    own. The service decides that — see ``app.domains.items.service``.
    """
    page = await items_service.list_items(
        db, viewer_id=user.id, mine=mine, limit=limit, offset=offset
    )
    return Page[ItemRead](
        items=[ItemRead.model_validate(item) for item in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{item_id}", response_model=ItemReadDetail)
async def read_item(
    item_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ItemReadDetail:
    """Return a single item.

    404 rather than 403 for an item the caller may not read, so the endpoint
    cannot be used to confirm which ids exist.
    """
    item = await items_service.get_item(db, item_id, viewer_id=user.id)
    return ItemReadDetail.model_validate(item)


@router.post("", response_model=ItemReadDetail, status_code=status.HTTP_201_CREATED)
async def create_item(
    data: ItemCreate,
    user: CurrentUser = Depends(require_permission(CREATE_ITEM)),
    db: AsyncSession = Depends(get_db),
) -> ItemReadDetail:
    """Create an item owned by the authenticated user."""
    item = await items_service.create_item(
        db,
        owner_id=user.id,
        name=data.name,
        price=data.price,
        description=data.description,
    )
    return ItemReadDetail.model_validate(item)


@router.patch("/{item_id}", response_model=ItemReadDetail)
async def update_item(
    item_id: uuid.UUID,
    data: ItemUpdate,
    user: CurrentUser = Depends(require_permission(UPDATE_ITEM)),
    db: AsyncSession = Depends(get_db),
) -> ItemReadDetail:
    """Update an item. The service enforces that the caller owns it."""
    item = await items_service.update_item(
        db,
        item_id=item_id,
        user_id=user.id,
        name=data.name,
        description=data.description,
        price=data.price,
    )
    return ItemReadDetail.model_validate(item)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    item_id: uuid.UUID,
    user: CurrentUser = Depends(require_permission(DELETE_ITEM)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete an item. The service enforces that the caller owns it."""
    await items_service.delete_item(db, item_id=item_id, user_id=user.id)
