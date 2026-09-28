"""Tests for the items reference domain.

The centre of gravity here is the **two-level authorization** model: holding
``UpdateItem`` must not let a user edit somebody else's item. That gap is the
classic IDOR vulnerability, and a permission check alone does not close it.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_api_student.domains.items import service as items_service
from cbpupsis_core.events import Event, event_bus
from cbpupsis_core.exceptions import ForbiddenError, NotFoundError


class TestItemService:
    async def test_create_and_read(self, db: AsyncSession) -> None:
        owner_id = uuid.uuid4()
        item = await items_service.create_item(
            db, owner_id=owner_id, name="Widget", price=Decimal("19.99")
        )
        assert item.owner_id == owner_id

        fetched = await items_service.get_item(db, item.id)
        assert fetched.name == "Widget"

    async def test_price_keeps_exact_decimal_value(self, db: AsyncSession) -> None:
        """The reason money is NUMERIC/Decimal: 0.1 + 0.2 must be 0.30 exactly."""
        item = await items_service.create_item(
            db, owner_id=uuid.uuid4(), name="Precise", price=Decimal("0.10")
        )
        assert Decimal(str(item.price)) + Decimal("0.20") == Decimal("0.30")

    async def test_missing_item_raises_not_found(self, db: AsyncSession) -> None:
        with pytest.raises(NotFoundError):
            await items_service.get_item(db, uuid.uuid4())

    async def test_soft_delete_hides_without_destroying(self, db: AsyncSession) -> None:
        owner_id = uuid.uuid4()
        item = await items_service.create_item(
            db, owner_id=owner_id, name="Doomed", price=Decimal("5.00")
        )
        await items_service.delete_item(db, item.id, owner_id)

        with pytest.raises(NotFoundError):
            await items_service.get_item(db, item.id)

        # The row survives for audit — soft delete, not a DELETE.
        raw = await db.get(items_service.Item, item.id)
        assert raw is not None and raw.deleted_at is not None

    async def test_list_excludes_soft_deleted(self, db: AsyncSession) -> None:
        owner_id = uuid.uuid4()
        keep = await items_service.create_item(
            db, owner_id=owner_id, name="Keep", price=Decimal("1.00")
        )
        drop = await items_service.create_item(
            db, owner_id=owner_id, name="Drop", price=Decimal("2.00")
        )
        await items_service.delete_item(db, drop.id, owner_id)

        page = await items_service.list_items(db, viewer_id=owner_id)
        assert page.total == 1
        assert [item.id for item in page.items] == [keep.id]

    async def test_list_paginates(self, db: AsyncSession) -> None:
        owner_id = uuid.uuid4()
        for index in range(5):
            await items_service.create_item(
                db, owner_id=owner_id, name=f"Item {index}", price=Decimal("1.00")
            )

        page = await items_service.list_items(db, viewer_id=owner_id, limit=2)
        assert len(page.items) == 2
        assert page.total == 5, "total counts all matches, not just this page"


class TestObjectLevelAuthorization:
    """The check a permission guard cannot make."""

    async def test_owner_may_update(self, db: AsyncSession) -> None:
        owner_id = uuid.uuid4()
        item = await items_service.create_item(
            db, owner_id=owner_id, name="Mine", price=Decimal("10.00")
        )
        updated = await items_service.update_item(db, item.id, owner_id, name="Renamed")
        assert updated.name == "Renamed"

    async def test_stranger_may_not_update(self, db: AsyncSession) -> None:
        """Even with UpdateItem granted, another user's item is off limits."""
        item = await items_service.create_item(
            db, owner_id=uuid.uuid4(), name="Not Yours", price=Decimal("10.00")
        )
        with pytest.raises(ForbiddenError):
            await items_service.update_item(db, item.id, uuid.uuid4(), name="Hijacked")

    async def test_stranger_may_not_delete(self, db: AsyncSession) -> None:
        item = await items_service.create_item(
            db, owner_id=uuid.uuid4(), name="Not Yours", price=Decimal("10.00")
        )
        with pytest.raises(ForbiddenError):
            await items_service.delete_item(db, item.id, uuid.uuid4())

    async def test_moderator_may_update_any_item(self, db: AsyncSession, grant) -> None:
        """The deliberate escape hatch: ModerateItem overrides ownership."""
        moderator_id = uuid.uuid4()
        await grant(moderator_id, "ModerateItem")
        item = await items_service.create_item(
            db, owner_id=uuid.uuid4(), name="Reported", price=Decimal("10.00")
        )

        updated = await items_service.update_item(
            db, item.id, moderator_id, name="Moderated"
        )
        assert updated.name == "Moderated"


class TestItemEndpoints:
    async def test_create_requires_permission(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.post(
            "/api/v1/items",
            json={"name": "Widget", "price": "19.99"},
            headers=auth_headers,
        )
        assert response.status_code == 403

    async def test_create_succeeds_with_permission(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        grant,
    ) -> None:
        await grant(uuid.UUID(registered_user["id"]), "CreateItem")
        response = await client.post(
            "/api/v1/items",
            json={"name": "Widget", "price": "19.99"},
            headers=auth_headers,
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Widget"
        assert body["owner_id"] == registered_user["id"], "owner comes from the token"

    async def test_owner_cannot_be_forged_via_the_body(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        grant,
    ) -> None:
        """extra='forbid' rejects an injected owner_id outright."""
        await grant(uuid.UUID(registered_user["id"]), "CreateItem")
        response = await client.post(
            "/api/v1/items",
            json={
                "name": "Widget",
                "price": "19.99",
                "owner_id": str(uuid.uuid4()),
            },
            headers=auth_headers,
        )
        assert response.status_code == 422

    async def test_negative_price_rejected(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        grant,
    ) -> None:
        await grant(uuid.UUID(registered_user["id"]), "CreateItem")
        response = await client.post(
            "/api/v1/items",
            json={"name": "Free", "price": "-5.00"},
            headers=auth_headers,
        )
        assert response.status_code == 422

    async def test_unauthenticated_list_rejected(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/items")).status_code == 401

    async def test_updating_another_users_item_is_forbidden(
        self,
        client: AsyncClient,
        db: AsyncSession,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        grant,
    ) -> None:
        """End-to-end IDOR check: the permission is held, the item is not."""
        await grant(uuid.UUID(registered_user["id"]), "UpdateItem")
        someone_else = await items_service.create_item(
            db, owner_id=uuid.uuid4(), name="Theirs", price=Decimal("10.00")
        )

        response = await client.patch(
            f"/api/v1/items/{someone_else.id}",
            json={"name": "Hijacked"},
            headers=auth_headers,
        )
        assert response.status_code == 403


class TestEvents:
    async def test_creating_an_item_publishes_an_event(self, db: AsyncSession) -> None:
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        event_bus.subscribe("item.created", handler)
        item = await items_service.create_item(
            db, owner_id=uuid.uuid4(), name="Watched", price=Decimal("1.00")
        )

        # Dispatch is in the background, so publish() returning does not mean
        # the handler has run — draining is what makes the assertion sound.
        # Asserting without it would pass or fail on scheduling luck.
        await event_bus.drain()

        assert len(received) == 1
        assert received[0].payload["item_id"] == str(item.id)

    async def test_publishing_does_not_wait_for_a_slow_handler(
        self, db: AsyncSession
    ) -> None:
        """The point of background dispatch: a slow listener must not be added
        to the latency of the request that triggered it."""
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(event: Event) -> None:
            started.set()
            await release.wait()

        event_bus.subscribe("item.created", slow)

        # Returns while the handler is still parked on release.wait().
        item = await items_service.create_item(
            db, owner_id=uuid.uuid4(), name="Fast", price=Decimal("1.00")
        )
        assert item.id is not None

        await asyncio.wait_for(started.wait(), timeout=1)
        release.set()
        await event_bus.drain()

    async def test_a_failing_handler_does_not_break_the_publisher(
        self, db: AsyncSession
    ) -> None:
        """A broken listener must not roll back the action that triggered it."""

        async def broken(event: Event) -> None:
            raise RuntimeError("handler is down")

        event_bus.subscribe("item.created", broken)
        item = await items_service.create_item(
            db, owner_id=uuid.uuid4(), name="Resilient", price=Decimal("1.00")
        )
        assert item.id is not None

        # Drained so the handler has actually run and raised by now: without
        # this the test would pass even if the failure were never isolated,
        # because the task would still be pending when the assertion ran.
        await event_bus.drain()


class TestItemsArePrivateByDefault:
    """Items are private: the owner filter is a boundary, not an opt-in.

    Before this, ``GET /items`` returned every user's rows to any authenticated
    caller and ``ReadAllItem`` was seeded but read by no code. These tests are
    the guard on that, and their absence is invisible — a leaking list endpoint
    returns 200 and looks perfectly healthy.
    """

    async def test_list_returns_only_the_callers_items(self, db: AsyncSession) -> None:
        viewer_id = uuid.uuid4()
        mine = await items_service.create_item(
            db, owner_id=viewer_id, name="Mine", price=Decimal("1.00")
        )
        await items_service.create_item(
            db, owner_id=uuid.uuid4(), name="Theirs", price=Decimal("2.00")
        )

        page = await items_service.list_items(db, viewer_id=viewer_id)
        assert [item.id for item in page.items] == [mine.id]
        assert page.total == 1, "total must describe the visible set, not the table"

    async def test_read_all_item_holder_sees_everyones(
        self, db: AsyncSession, grant
    ) -> None:
        viewer_id = uuid.uuid4()
        await items_service.create_item(
            db, owner_id=viewer_id, name="Mine", price=Decimal("1.00")
        )
        await items_service.create_item(
            db, owner_id=uuid.uuid4(), name="Theirs", price=Decimal("2.00")
        )
        await grant(viewer_id, "ReadAllItem")

        page = await items_service.list_items(db, viewer_id=viewer_id)
        assert page.total == 2

    async def test_mine_narrows_a_read_all_holder_to_their_own(
        self, db: AsyncSession, grant
    ) -> None:
        """``mine`` survives the change — it is now a narrowing filter for the
        only caller for whom it was ever meaningful."""
        viewer_id = uuid.uuid4()
        mine = await items_service.create_item(
            db, owner_id=viewer_id, name="Mine", price=Decimal("1.00")
        )
        await items_service.create_item(
            db, owner_id=uuid.uuid4(), name="Theirs", price=Decimal("2.00")
        )
        await grant(viewer_id, "ReadAllItem")

        page = await items_service.list_items(db, viewer_id=viewer_id, mine=True)
        assert [item.id for item in page.items] == [mine.id]

    async def test_get_someone_elses_item_is_404_not_403(
        self, db: AsyncSession
    ) -> None:
        """404, so the endpoint cannot be used to confirm which ids exist."""
        theirs = await items_service.create_item(
            db, owner_id=uuid.uuid4(), name="Theirs", price=Decimal("2.00")
        )

        with pytest.raises(NotFoundError):
            await items_service.get_item(db, theirs.id, viewer_id=uuid.uuid4())

    async def test_read_all_item_holder_may_get_someone_elses(
        self, db: AsyncSession, grant
    ) -> None:
        viewer_id = uuid.uuid4()
        theirs = await items_service.create_item(
            db, owner_id=uuid.uuid4(), name="Theirs", price=Decimal("2.00")
        )
        await grant(viewer_id, "ReadAllItem")

        fetched = await items_service.get_item(db, theirs.id, viewer_id=viewer_id)
        assert fetched.id == theirs.id

    async def test_moderator_update_is_unaffected_by_the_read_boundary(
        self, db: AsyncSession, grant
    ) -> None:
        """The internal load must not 404 a moderator before their own check.

        ``update_item``/``delete_item`` authorize the loaded row against
        ``ModerateItem``, which is strictly stronger than the read boundary.
        Passing a viewer id into that load would deny them at the wrong layer.
        """
        moderator_id = uuid.uuid4()
        theirs = await items_service.create_item(
            db, owner_id=uuid.uuid4(), name="Theirs", price=Decimal("2.00")
        )
        await grant(moderator_id, "ModerateItem")

        updated = await items_service.update_item(
            db, theirs.id, moderator_id, name="Moderated"
        )
        assert updated.name == "Moderated"


class TestItemVisibilityOverHTTP:
    async def test_list_does_not_leak_another_users_items(
        self, client: AsyncClient, db: AsyncSession, auth_headers: dict[str, str]
    ) -> None:
        await items_service.create_item(
            db, owner_id=uuid.uuid4(), name="Someone else's", price=Decimal("5.00")
        )

        response = await client.get("/api/v1/items", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["total"] == 0, response.text

    async def test_get_another_users_item_is_404(
        self, client: AsyncClient, db: AsyncSession, auth_headers: dict[str, str]
    ) -> None:
        theirs = await items_service.create_item(
            db, owner_id=uuid.uuid4(), name="Someone else's", price=Decimal("5.00")
        )

        response = await client.get(f"/api/v1/items/{theirs.id}", headers=auth_headers)
        assert response.status_code == 404, response.text


class TestPaginationOrdering:
    async def test_adjacent_pages_do_not_repeat_a_row(self, db: AsyncSession) -> None:
        """Rows sharing a ``created_at`` need a unique tiebreaker.

        In-memory SQLite stamps these fast enough to collide, which is exactly
        the condition that makes an unstable sort repeat or skip a row across
        two page queries.
        """
        viewer_id = uuid.uuid4()
        for index in range(6):
            await items_service.create_item(
                db, owner_id=viewer_id, name=f"Item {index}", price=Decimal("1.00")
            )

        first = await items_service.list_items(db, viewer_id=viewer_id, limit=3)
        second = await items_service.list_items(
            db, viewer_id=viewer_id, limit=3, offset=3
        )

        ids = [item.id for item in first.items] + [item.id for item in second.items]
        assert len(set(ids)) == 6, "a row appeared on both pages, or was skipped"


class TestTheClientSeamRespectsTheBoundary:
    """`client.py` is the cross-domain entry point, so the boundary has to hold
    there too — otherwise the first domain to call it reopens the leak."""

    async def test_client_applies_the_read_boundary_when_given_a_viewer(
        self, db: AsyncSession
    ) -> None:
        from cbpupsis_api_student.domains.items import client as items_client

        theirs = await items_service.create_item(
            db, owner_id=uuid.uuid4(), name="Theirs", price=Decimal("2.00")
        )

        with pytest.raises(NotFoundError):
            await items_client.get_item(db, theirs.id, viewer_id=uuid.uuid4())

    async def test_client_returns_the_owners_own_item(self, db: AsyncSession) -> None:
        from cbpupsis_api_student.domains.items import client as items_client

        owner_id = uuid.uuid4()
        mine = await items_service.create_item(
            db, owner_id=owner_id, name="Mine", price=Decimal("2.00")
        )

        dto = await items_client.get_item(db, mine.id, viewer_id=owner_id)
        assert dto.id == mine.id
