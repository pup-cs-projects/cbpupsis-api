"""Repository-layer tests for the items domain.

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
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.items import repository as items_repository


class TestItemsRepository:
    """The reference shape: soft-delete filtering and (rows, total) paging."""

    async def test_get_item_returns_none_when_absent(self, db: AsyncSession) -> None:
        """The whole contract in one line: absence is None, never an exception."""
        assert await items_repository.get_item(db, uuid.uuid4()) is None

    async def test_get_item_returns_none_for_soft_deleted_row(
        self, db: AsyncSession, make_user
    ) -> None:
        """A soft-deleted row is invisible to reads, not merely flagged."""
        user = await make_user("items-owner@example.com")
        item = items_repository.add_item(
            db, owner_id=user.id, name="Widget", price=Decimal("9.99")
        )
        await db.commit()

        items_repository.delete_item(db, item, datetime.now(UTC))
        await db.commit()

        assert await items_repository.get_item(db, item.id) is None

    async def test_list_items_total_counts_all_matches_not_the_page(
        self, db: AsyncSession, make_user
    ) -> None:
        """``total`` describes the full result set; the rows are just one page."""
        user = await make_user("pager@example.com")
        for n in range(5):
            items_repository.add_item(
                db, owner_id=user.id, name=f"Item {n}", price=Decimal("1.00")
            )
        await db.commit()

        rows, total = await items_repository.list_items(
            db, owner_id=None, limit=2, offset=0
        )
        assert len(rows) == 2
        assert total == 5

    async def test_list_items_filters_by_owner(
        self, db: AsyncSession, make_user
    ) -> None:
        owner = await make_user("owner@example.com")
        other = await make_user("other@example.com")
        items_repository.add_item(
            db, owner_id=owner.id, name="Mine", price=Decimal("1.00")
        )
        items_repository.add_item(
            db, owner_id=other.id, name="Theirs", price=Decimal("1.00")
        )
        await db.commit()

        rows, total = await items_repository.list_items(
            db, owner_id=owner.id, limit=50, offset=0
        )
        assert total == 1
        assert rows[0].name == "Mine"

    async def test_add_item_does_not_commit(self, db: AsyncSession, make_user) -> None:
        """The repository stages; the service decides the unit of work is done.

        Without this, a multi-step operation could not be made atomic — an early
        step would already be durable when a later one failed.
        """
        user = await make_user("rollback@example.com")
        item = items_repository.add_item(
            db, owner_id=user.id, name="Uncommitted", price=Decimal("5.00")
        )
        item_id = item.id
        await db.rollback()

        assert await items_repository.get_item(db, item_id) is None
