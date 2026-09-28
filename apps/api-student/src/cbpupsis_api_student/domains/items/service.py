"""Business logic for the items domain — its framework-agnostic public interface.

This module demonstrates the **second level of authorization**, and it is the
half that endpoint dependencies cannot cover:

- ``require_permission(UPDATE_ITEM)`` in the router answers *"may this user
  update items at all?"*
- ``_require_owner_or_permission`` here answers *"may they update THIS item?"*

Only the second needs the loaded row, so only the second can live in the service
layer. Enforcing just the first is how applications ship IDOR vulnerabilities —
any authenticated user with a generic permission could edit every other user's
records by changing an id in the URL.

No SQL lives here: every read and write goes through ``repository.py``. This
module owns the rules and the transaction boundary — it decides when a unit of
work is complete and commits it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.items import repository
from cbpupsis_api_student.domains.items.constants import MODERATE_ITEM, READ_ALL_ITEM
from cbpupsis_api_student.domains.items.exceptions import (
    ItemAccessDeniedError,
    ItemNotFoundError,
)
from cbpupsis_core.events import Event, event_bus
from cbpupsis_core.pagination import Page
from cbpupsis_database.models.items import Item
from cbpupsis_shared.domains.iam import service as iam_service


async def _require_owner_or_permission(
    db: AsyncSession, item: Item, user_id: uuid.UUID, permission: str
) -> None:
    """Allow the item's owner, or a user holding ``permission`` (e.g. a moderator).

    Raises :class:`ForbiddenError` otherwise.
    """
    if item.owner_id == user_id:
        return
    granted = await iam_service.get_effective_permissions(db, user_id)
    if permission not in granted:
        raise ItemAccessDeniedError


async def get_item(
    db: AsyncSession, item_id: uuid.UUID, viewer_id: uuid.UUID | None = None
) -> Item:
    """Return a live item by id, or raise :class:`NotFoundError`.

    The repository returns ``None`` for a missing row; turning that absence into
    a 404 is a policy decision, which is why it happens here and not there.

    ``viewer_id`` applies the read boundary: items are private, so a viewer sees
    their own items and, with ``ReadAllItem``, everyone's. An item the viewer
    may not read raises :class:`ItemNotFoundError`, **not** a 403 — a 403 here
    would confirm the id exists and turn the endpoint into an enumeration
    oracle, which is exactly the case that error's docstring reserves the 404
    for.

    Left optional because the internal callers below (update, delete) authorize
    the loaded row themselves against ``ModerateItem``, a strictly stronger
    check: passing ``viewer_id`` there would 404 a moderator before they ever
    reached it.
    """
    item = await repository.get_item(db, item_id)
    if item is None:
        raise ItemNotFoundError(item_id)
    if viewer_id is not None and item.owner_id != viewer_id:
        granted = await iam_service.get_effective_permissions(db, viewer_id)
        if READ_ALL_ITEM not in granted:
            raise ItemNotFoundError(item_id)
    return item


async def list_items(
    db: AsyncSession,
    viewer_id: uuid.UUID,
    mine: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> Page[Item]:
    """Return a page of items the viewer is allowed to see.

    **Items are private by default.** The owner filter is a boundary, not an
    opt-in: a caller without ``ReadAllItem`` is scoped to their own rows no
    matter what they ask for. A holder of ``ReadAllItem`` sees everyone's, and
    ``mine`` narrows them back to their own — which is why ``mine`` stays a
    parameter rather than being deleted along with the old behaviour.

    Deriving the filter here rather than in the router is the point: a rule
    enforced at the edge is one a background job or another domain walks past,
    and the router cannot express "unless they hold this permission" as a
    dependency, because the answer changes which rows are returned rather than
    whether the call is allowed.

    Always paginated: an unbounded list endpoint is a latency and memory problem
    that only appears once the table is large, i.e. in production.

    The repository returns ``(rows, total)``; wrapping that in the ``Page``
    envelope is this layer's job, because the envelope is part of the domain's
    outward contract rather than part of data access.
    """
    if mine:
        owner_id: uuid.UUID | None = viewer_id
    else:
        granted = await iam_service.get_effective_permissions(db, viewer_id)
        owner_id = None if READ_ALL_ITEM in granted else viewer_id

    rows, total = await repository.list_items(
        db, owner_id=owner_id, limit=limit, offset=offset
    )
    return Page(items=rows, total=total, limit=limit, offset=offset)


async def create_item(
    db: AsyncSession,
    owner_id: uuid.UUID,
    name: str,
    price: Decimal,
    description: str | None = None,
) -> Item:
    """Create an item owned by ``owner_id`` and announce it."""
    item = repository.add_item(
        db, owner_id=owner_id, name=name, price=price, description=description
    )
    await db.commit()
    await db.refresh(item)

    # Emitted, not called inline: this domain does not know or care who reacts.
    # In-process today; the same call goes to a broker after a service split.
    await event_bus.publish(
        Event(
            name="item.created",
            payload={"item_id": str(item.id), "owner_id": str(owner_id)},
        )
    )
    return item


async def update_item(
    db: AsyncSession,
    item_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str | None = None,
    description: str | None = None,
    price: Decimal | None = None,
) -> Item:
    """Update an item the caller owns (or may moderate)."""
    item = await get_item(db, item_id)
    await _require_owner_or_permission(db, item, user_id, MODERATE_ITEM)

    repository.update_item(db, item, name=name, description=description, price=price)
    await db.commit()
    await db.refresh(item)
    return item


async def delete_item(db: AsyncSession, item_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Soft-delete an item the caller owns (or may moderate)."""
    item = await get_item(db, item_id)
    await _require_owner_or_permission(db, item, user_id, MODERATE_ITEM)

    repository.delete_item(db, item, datetime.now(UTC))
    await db.commit()
