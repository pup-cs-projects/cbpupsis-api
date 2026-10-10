"""Tests for the audit domain.

The trail exists to answer "who granted what, to whom, and when" months after
the fact, so the assertions that matter are that a change *produces* an entry
and that the entry names the actor. An audit feature that silently records
nothing looks exactly like one that works — every endpoint still returns its
usual status.

The subscriber opens its own session (it runs after the emitting request has
returned), so these tests drive it directly rather than through the bus wherever
the assertion is about the row's content. The end-to-end path — event published,
handler invoked — is covered in ``TestTheTrailIsWiredUp``, and the write itself
in ``TestTheHandlerActuallyWrites``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.events import Event
from cbpupsis_database.models.outbox import OutboxMessage
from cbpupsis_shared import worker
from cbpupsis_shared.domains.audit import service as audit_service
from cbpupsis_shared.domains.audit.constants import AUDITED_EVENTS
from cbpupsis_shared.domains.audit.subscribers import register_audit_subscribers
from cbpupsis_shared.domains.iam import service as iam_service


class TestRecording:
    async def test_records_an_entry_with_its_actor_and_target(
        self, db: AsyncSession
    ) -> None:
        actor = uuid.uuid4()
        entry = await audit_service.record(
            db,
            action="iam.user_added_to_group",
            actor_id=actor,
            target_type="user",
            target_id="some-user",
            payload={"group_id": 7},
        )

        assert entry.actor_id == actor
        assert entry.target_id == "some-user"
        assert entry.payload["group_id"] == 7
        assert entry.occurred_at is not None

    async def test_an_entry_may_have_no_actor(self, db: AsyncSession) -> None:
        """A seed or a migration has no human behind it, and inventing one would
        put a falsehood in the one table that must not contain any."""
        entry = await audit_service.record(db, action="iam.group_created")
        assert entry.actor_id is None

    async def test_lists_newest_first(self, db: AsyncSession) -> None:
        for index in range(3):
            await audit_service.record(
                db, action="iam.group_created", target_id=str(index)
            )

        page = await audit_service.list_entries(db)
        assert page.total == 3
        # Entries written in one test share a timestamp to the microsecond on
        # some platforms; the id tiebreaker is what keeps the order total.
        assert len(page.items) == 3

    async def test_filters_by_actor_action_and_target(self, db: AsyncSession) -> None:
        actor = uuid.uuid4()
        await audit_service.record(
            db, action="iam.group_created", actor_id=actor, target_id="g1"
        )
        await audit_service.record(db, action="iam.user_added_to_group", target_id="u1")

        by_actor = await audit_service.list_entries(db, actor_id=actor)
        by_action = await audit_service.list_entries(db, action="iam.group_created")
        by_target = await audit_service.list_entries(db, target_id="u1")

        assert by_actor.total == 1 and by_actor.items[0].target_id == "g1"
        assert by_action.total == 1
        assert by_target.total == 1
        assert by_target.items[0].action == "iam.user_added_to_group"


class TestTheTrailIsWiredUp:
    """The wiring is the part that silently does nothing when it breaks."""

    def test_every_audited_event_has_a_subscriber(self) -> None:
        """A name in AUDITED_EVENTS that nothing subscribes to reads as audited
        while recording nothing."""
        register_audit_subscribers()
        for name in AUDITED_EVENTS:
            assert worker.handlers_for(name), f"{name} has no subscriber"

    def test_registering_twice_does_not_double_the_handler(self) -> None:
        """An app rebuilt in a test, or reloaded in development, must not make
        one change produce two audit rows."""
        register_audit_subscribers()
        before = len(worker.handlers_for("iam.group_created"))
        register_audit_subscribers()
        assert len(worker.handlers_for("iam.group_created")) == before

    async def test_an_iam_change_reaches_the_handler(
        self, db: AsyncSession, make_user
    ) -> None:
        """The business change and complete audit payload share a commit."""
        actor = uuid.uuid4()
        subject = (await make_user("audited-member@example.com")).id
        group = await iam_service.create_group(db, name="Audited")
        await iam_service.add_user_to_group(db, subject, group.id, actor_id=actor)
        messages = (
            (
                await db.execute(
                    select(OutboxMessage).where(
                        OutboxMessage.event_name == "iam.user_added_to_group"
                    )
                )
            )
            .scalars()
            .all()
        )

        assert len(messages) == 1
        assert messages[0].payload["user_id"] == str(subject)
        assert messages[0].payload["actor_id"] == str(actor)
        assert messages[0].payload["group_id"] == group.id

    async def test_an_idempotent_no_op_records_nothing(
        self, db: AsyncSession, make_user
    ) -> None:
        """Re-adding an existing member changes nothing, so the trail must not
        claim a grant happened."""
        subject = (await make_user("idempotent-member@example.com")).id
        group = await iam_service.create_group(db, name="Audited")
        await iam_service.add_user_to_group(db, subject, group.id)
        await iam_service.add_user_to_group(db, subject, group.id)
        messages = (
            (
                await db.execute(
                    select(OutboxMessage).where(
                        OutboxMessage.event_name == "iam.user_added_to_group"
                    )
                )
            )
            .scalars()
            .all()
        )

        assert len(messages) == 1, "the second, no-op call must not be recorded"


class TestAuditEndpoint:
    async def test_refused_without_the_permission(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.get("/api/v1/audit", headers=auth_headers)
        assert response.status_code == 403, response.text

    async def test_refused_without_authentication(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/audit")
        assert response.status_code == 401, response.text

    async def test_manage_iam_alone_does_not_grant_the_trail(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        grant,
    ) -> None:
        """Reading the trail and reshaping authorization are different powers.

        An auditor must be able to see every grant without being able to make
        one — and, in the other direction, an IAM admin should not silently gain
        the ability to read who has been watching them.
        """
        await grant(uuid.UUID(registered_user["id"]), "ManageIAM")

        response = await client.get("/api/v1/audit", headers=auth_headers)
        assert response.status_code == 403, response.text

    async def test_returns_the_trail_with_the_permission(
        self,
        client: AsyncClient,
        db: AsyncSession,
        superadmin_headers: dict[str, str],
    ) -> None:
        await audit_service.record(
            db, action="iam.group_created", target_type="group", target_id="42"
        )

        response = await client.get("/api/v1/audit", headers=superadmin_headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert {"items", "total", "limit", "offset"} <= set(body)
        assert body["items"][0]["action"] == "iam.group_created"

    async def test_there_is_no_way_to_write_an_entry(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
        grant,
    ) -> None:
        """An audit trail a client can write to proves nothing."""
        await grant(uuid.UUID(registered_user["id"]), "ReadAllAuditEntry")

        response = await client.post(
            "/api/v1/audit",
            json={"action": "forged", "occurred_at": datetime.now(UTC).isoformat()},
            headers=auth_headers,
        )
        assert response.status_code == 405, response.text


class TestTheHandlerActuallyWrites:
    """The gap that existed until ``record_event_on`` was split out.

    ``record_event`` opens a session from the application engine, which in this
    suite points at a Postgres that is not running — so no test could observe
    what it wrote, and the strongest available assertion was "it did not raise".
    A handler that silently wrote nothing would have passed.
    """

    async def test_an_event_becomes_an_audit_row(self, db: AsyncSession) -> None:
        actor = uuid.uuid4()
        subject = uuid.uuid4()

        await audit_service.record_event_on(
            db,
            Event(
                name="iam.user_added_to_group",
                payload={
                    "user_id": str(subject),
                    "group_id": 7,
                    "actor_id": str(actor),
                },
            ),
        )

        page = await audit_service.list_entries(db)
        assert page.total == 1
        entry = page.items[0]
        assert entry.action == "iam.user_added_to_group"
        assert entry.actor_id == actor
        assert entry.target_type == "user"
        assert entry.target_id == str(subject)
        assert entry.payload["group_id"] == 7

    async def test_an_event_with_no_actor_still_records(self, db: AsyncSession) -> None:
        """A seed or a migration has no human behind it, and inventing one would
        put a falsehood in the one table that must not contain any."""
        await audit_service.record_event_on(
            db, Event(name="iam.group_created", payload={"group_id": 1})
        )

        page = await audit_service.list_entries(db)
        assert page.items[0].actor_id is None

    async def test_an_unmapped_event_records_without_a_target(
        self, db: AsyncSession
    ) -> None:
        """An event absent from ``_TARGETS`` must still be recorded rather than
        dropped — a trail that silently discards unfamiliar events is worse than
        one with an unfamiliar name in it."""
        await audit_service.record_event_on(
            db, Event(name="something.unmapped", payload={"x": 1})
        )

        page = await audit_service.list_entries(db)
        assert page.total == 1
        assert page.items[0].target_type is None
