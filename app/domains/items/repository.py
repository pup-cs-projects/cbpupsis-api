"""Data access for the items domain. CRUD only: no rules, no authorization.

This module is the only place in the domain that builds a query. It returns ORM
rows, scalars, or ``(rows, total)`` tuples — never Pydantic schemas, and never a
domain error. A lookup that finds nothing returns ``None``; deciding that the
absence means :class:`~app.core.exceptions.NotFoundError` is the service's job.

That boundary is what makes this layer reusable: the same ``get_item`` serves
the read endpoint (which 404s), the update path (which also checks ownership),
and a background job (which may legitimately skip a missing row). Baking the
raise in here would force every caller to catch it back out.

Transactions belong to the service: nothing here commits. The service knows when
a unit of work is complete, and a repository that committed per call would make
multi-step operations impossible to make atomic. ``flush()`` is allowed where a
server-generated id is needed before the transaction ends.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.items.models import Item


def _live() -> Select[tuple[Item]]:
    """Base query excluding soft-deleted rows.

    Centralised so no caller has to remember the ``deleted_at IS NULL`` filter —
    forgetting it silently resurrects deleted records.
    """
    return select(Item).where(Item.deleted_at.is_(None))


async def get_item(db: AsyncSession, item_id: uuid.UUID) -> Item | None:
    """Return one live item by id, or ``None`` if there is no such row.

    Returning ``None`` rather than raising is deliberate — see the module
    docstring. Translating it into a 404 is the service's decision.

    SQL::

        SELECT items.owner_id, items.name, items.description, items.price,
               items.id, items.created_at, items.updated_at, items.deleted_at
        FROM items
        WHERE items.deleted_at IS NULL AND items.id = :id_1::UUID
    """
    return await db.scalar(_live().where(Item.id == item_id))


async def list_items(
    db: AsyncSession,
    owner_id: uuid.UUID | None,
    limit: int,
    offset: int,
) -> tuple[list[Item], int]:
    """Return one page of live items and the total matching count.

    The count is taken from the same filtered query as the page, before limit
    and offset are applied, so ``total`` describes the whole result set rather
    than the slice returned — that is the number a client needs to render paging.

    Ordered newest-first, with ``Item.id`` as a tiebreaker. The second key is
    not decoration: ``created_at`` alone is not unique, and rows sharing one
    have no guaranteed relative order between two queries — so a row can appear
    on both page 1 and page 2, or on neither, while nothing looks wrong. The id
    is unique, so the total order it produces is stable across the separate
    queries that fetch adjacent pages.

    Two statements. The ``owner_id`` predicate shown in both is present only
    when ``owner_id`` is not ``None``; otherwise each keeps just the
    ``deleted_at IS NULL`` filter.

    SQL::

        -- 1. total matching rows, before limit/offset
        SELECT count(*) AS count_1
        FROM (SELECT items.owner_id AS owner_id, items.name AS name,
                     items.description AS description, items.price AS price,
                     items.id AS id, items.created_at AS created_at,
                     items.updated_at AS updated_at,
                     items.deleted_at AS deleted_at
              FROM items
              WHERE items.deleted_at IS NULL
                AND items.owner_id = :owner_id_1::UUID) AS anon_1

        -- 2. the page itself
        SELECT items.owner_id, items.name, items.description, items.price,
               items.id, items.created_at, items.updated_at, items.deleted_at
        FROM items
        WHERE items.deleted_at IS NULL AND items.owner_id = :owner_id_1::UUID
        ORDER BY items.created_at DESC, items.id DESC
        LIMIT :param_1 OFFSET :param_2
    """
    query = _live()
    if owner_id is not None:
        query = query.where(Item.owner_id == owner_id)

    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    result = await db.execute(
        query.order_by(Item.created_at.desc(), Item.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), total or 0


def add_item(
    db: AsyncSession,
    owner_id: uuid.UUID,
    name: str,
    price: Decimal,
    description: str | None = None,
) -> Item:
    """Stage a new item on the session and return it.

    Synchronous because ``Session.add`` does not touch the database — the INSERT
    happens when the service commits. Typed as a plain function rather than a
    coroutine so that is visible at the call site instead of implying I/O that
    does not occur here.

    Emits no SQL here. On the service's commit::

        INSERT INTO items (owner_id, name, description, price, id, deleted_at)
        VALUES (:owner_id::UUID, :name, :description, :price, :id::UUID,
                :deleted_at)
        RETURNING items.created_at, items.updated_at
    """
    item = Item(owner_id=owner_id, name=name, price=price, description=description)
    db.add(item)
    return item


def update_item(
    db: AsyncSession,
    item: Item,
    *,
    name: str | None = None,
    description: str | None = None,
    price: Decimal | None = None,
) -> Item:
    """Apply a partial update to a loaded item and return it.

    Only arguments that are not ``None`` are written, so a PATCH that omits a
    field leaves it alone. The caller passes a row it already loaded because the
    service has to authorize against that row first; re-fetching here would be a
    wasted round-trip and would open a window between the check and the write.

    Emits no SQL here. On the service's commit, with only the columns actually
    changed in the SET list — the variant below is the all-three-arguments case::

        UPDATE items
        SET name=:name, description=:description, price=:price,
            updated_at=now()
        WHERE items.id = :id_1::UUID
    """
    if name is not None:
        item.name = name
    if description is not None:
        item.description = description
    if price is not None:
        item.price = price
    return item


def delete_item(db: AsyncSession, item: Item, deleted_at: datetime) -> Item:
    """Soft-delete a loaded item by stamping ``deleted_at``.

    The timestamp is passed in rather than generated here so the service decides
    the instant, and so a caller soft-deleting several rows in one unit of work
    can give them all the same one.

    A Python datetime, not ``func.now()``: a SQL expression would leave the
    attribute expired and trigger a lazy reload on next access, which async
    SQLAlchemy cannot perform outside an await (MissingGreenlet).

    Emits no SQL here. On the service's commit::

        UPDATE items
        SET updated_at=now(), deleted_at=:deleted_at
        WHERE items.id = :id_1::UUID
    """
    item.deleted_at = deleted_at
    return item
