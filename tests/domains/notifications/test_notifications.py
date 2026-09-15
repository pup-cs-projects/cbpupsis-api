"""Tests for the notifications domain.

Three groups of assertion matter here, and each covers a failure that is
invisible from the outside:

- **Ownership.** A list endpoint that filters by nothing returns 200 with
  somebody else's data. The tests scope every read to the caller and check that
  another user's notification answers 404, not 403.
- **Idempotency.** Delivery is at-least-once, so the same message *will* be
  redelivered after a crash. Dispatching one twice must produce one notification
  and one email — proven here by doing exactly that.
- **Mandatory notices.** A security alert that a user (or an attacker holding
  their session) can switch off is not an alert. Those types are refused.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event
from app.core.outbox import DeliveryReceipt, publish_transactional
from app.domains.notifications import handlers, repository
from app.domains.notifications import service as notifications_service
from app.domains.notifications.channels import (
    CHANNELS,
    Channel,
    EmailChannel,
    EmailDeliveryError,
    InAppChannel,
    Recipient,
    RenderedNotification,
)
from app.domains.notifications.constants import (
    NOTIFICATION_TYPES,
)
from app.domains.notifications.constants import (
    Channel as ChannelName,
)
from app.domains.notifications.exceptions import (
    MandatoryNotificationError,
    NotificationNotFoundError,
    UnknownNotificationTypeError,
)
from app.domains.notifications.models import Notification


async def _notify(db: AsyncSession, user_id: uuid.UUID, **kw) -> Notification:
    notification = repository.add_notification(
        db,
        user_id=user_id,
        notification_type=kw.pop("notification_type", "item.created"),
        title=kw.pop("title", "Hello"),
        body=kw.pop("body", "Something happened"),
        **kw,
    )
    await db.commit()
    return notification


class TestNotificationService:
    async def test_lists_only_the_callers_notifications(self, db: AsyncSession) -> None:
        mine = uuid.uuid4()
        await _notify(db, mine)
        await _notify(db, uuid.uuid4())

        page = await notifications_service.list_for_user(db, mine)

        assert page.total == 1
        assert page.items[0].user_id == mine

    async def test_counts_only_unread(self, db: AsyncSession) -> None:
        user_id = uuid.uuid4()
        first = await _notify(db, user_id)
        await _notify(db, user_id)

        assert await notifications_service.unread_count(db, user_id) == 2
        await notifications_service.mark_read(db, first.id, user_id)
        assert await notifications_service.unread_count(db, user_id) == 1

    async def test_marking_read_is_idempotent(self, db: AsyncSession) -> None:
        """The meaningful fact is when they FIRST saw it, so re-reading must not
        rewrite the timestamp."""
        user_id = uuid.uuid4()
        notification = await _notify(db, user_id)

        first = await notifications_service.mark_read(db, notification.id, user_id)
        stamp = first.read_at
        again = await notifications_service.mark_read(db, notification.id, user_id)

        assert again.read_at == stamp

    async def test_mark_all_read_clears_the_badge(self, db: AsyncSession) -> None:
        user_id = uuid.uuid4()
        for _ in range(3):
            await _notify(db, user_id)

        updated = await notifications_service.mark_all_read(db, user_id)

        assert updated == 3
        assert await notifications_service.unread_count(db, user_id) == 0

    async def test_mark_all_read_leaves_other_users_alone(
        self, db: AsyncSession
    ) -> None:
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        await _notify(db, mine)
        await _notify(db, theirs)

        await notifications_service.mark_all_read(db, mine)

        assert await notifications_service.unread_count(db, theirs) == 1


class TestOwnership:
    """A notification is private to its recipient. There is no elevated read
    path here at all — the service takes the viewer's id and scopes by it."""

    async def test_another_users_notification_is_404_not_403(
        self, db: AsyncSession
    ) -> None:
        """404 so the endpoint cannot be used to probe which ids exist, which
        would leak how much activity other people have."""
        theirs = await _notify(db, uuid.uuid4())

        with pytest.raises(NotificationNotFoundError):
            await notifications_service.get_for_user(db, theirs.id, uuid.uuid4())

    async def test_a_missing_notification_raises_the_same_error(
        self, db: AsyncSession
    ) -> None:
        """Indistinguishable from someone else's, deliberately."""
        with pytest.raises(NotificationNotFoundError):
            await notifications_service.get_for_user(db, uuid.uuid4(), uuid.uuid4())

    async def test_cannot_mark_another_users_notification_read(
        self, db: AsyncSession
    ) -> None:
        theirs = await _notify(db, uuid.uuid4())

        with pytest.raises(NotificationNotFoundError):
            await notifications_service.mark_read(db, theirs.id, uuid.uuid4())


class TestPreferences:
    async def test_absence_means_the_default(self, db: AsyncSession) -> None:
        """A type added after a user signed up must apply to them immediately —
        no backfill, and nobody silently excluded for predating the feature."""
        channels = await notifications_service.resolve_channels(
            db, uuid.uuid4(), "item.created"
        )

        assert channels == NOTIFICATION_TYPES["item.created"].default_channels

    async def test_disabling_a_channel_removes_it(self, db: AsyncSession) -> None:
        user_id = uuid.uuid4()

        await notifications_service.set_preference(
            db, user_id, "item.created", ChannelName.IN_APP, enabled=False
        )

        channels = await notifications_service.resolve_channels(
            db, user_id, "item.created"
        )
        assert ChannelName.IN_APP not in channels

    async def test_a_security_notice_cannot_be_disabled(self, db: AsyncSession) -> None:
        """The assertion that keeps an account-takeover alert reaching its
        victim. An attacker holding a session must not be able to silence it."""
        with pytest.raises(MandatoryNotificationError):
            await notifications_service.set_preference(
                db,
                uuid.uuid4(),
                "user.deactivated",
                ChannelName.EMAIL,
                enabled=False,
            )

    async def test_a_security_notice_ignores_a_stored_preference(
        self, db: AsyncSession
    ) -> None:
        """Belt and braces: even a row written by a script or a migration —
        bypassing set_preference entirely — must not suppress it."""
        user_id = uuid.uuid4()
        repository.add_preference(
            db, user_id, "user.deactivated", ChannelName.EMAIL, enabled=False
        )
        await db.commit()

        channels = await notifications_service.resolve_channels(
            db, user_id, "user.deactivated"
        )
        assert ChannelName.EMAIL in channels

    async def test_an_unknown_type_is_refused(self, db: AsyncSession) -> None:
        """Storing a preference for a type nothing emits would let a user
        believe they had turned something off."""
        with pytest.raises(UnknownNotificationTypeError):
            await notifications_service.set_preference(
                db, uuid.uuid4(), "nope.nothing", ChannelName.EMAIL, enabled=False
            )

    async def test_preferences_report_every_known_type(self, db: AsyncSession) -> None:
        """A settings screen needs the effective state, not just the overrides —
        otherwise a user who has changed nothing sees an empty page."""
        prefs = await notifications_service.get_preferences(db, uuid.uuid4())

        types = {p.notification_type for p in prefs}
        assert types == set(NOTIFICATION_TYPES)

    async def test_a_mandatory_type_is_reported_as_not_editable(
        self, db: AsyncSession
    ) -> None:
        """So a client renders the toggle disabled instead of offering a switch
        the API will refuse."""
        prefs = await notifications_service.get_preferences(db, uuid.uuid4())

        mandatory = [p for p in prefs if p.notification_type == "user.deactivated"]
        assert mandatory and all(not p.editable for p in mandatory)


class TestChannels:
    def test_every_channel_satisfies_the_protocol(self) -> None:
        """What makes "adding push is a new class" true rather than aspirational."""
        for name, channel in CHANNELS.items():
            assert isinstance(channel, Channel), name
            assert channel.name == name

    async def test_the_in_app_channel_writes_a_row(self, db: AsyncSession) -> None:
        user_id = uuid.uuid4()

        await InAppChannel().deliver(
            db,
            recipient=Recipient(user_id=user_id),
            message=RenderedNotification("item.created", "T", "B", {}),
        )

        assert await notifications_service.unread_count(db, user_id) == 1

    async def test_the_email_channel_sends(self, db: AsyncSession, monkeypatch) -> None:
        sent: list[dict] = []

        async def _capture(*, to: str, subject: str, body: str) -> bool:
            sent.append({"to": to, "subject": subject})
            return True

        monkeypatch.setattr("app.domains.notifications.channels.send_email", _capture)

        await EmailChannel().deliver(
            db,
            recipient=Recipient(user_id=uuid.uuid4(), email="x@example.com"),
            message=RenderedNotification("item.created", "Subject", "Body", {}),
        )

        assert sent == [{"to": "x@example.com", "subject": "Subject"}]

    async def test_a_failed_send_raises_so_the_outbox_retries(
        self, db: AsyncSession, monkeypatch
    ) -> None:
        """``send_email`` reports failure as False and never raises, which is
        right for a request path and wrong here: the outbox must learn of the
        failure or it marks the message delivered."""

        async def _fails(*, to: str, subject: str, body: str) -> bool:
            return False

        monkeypatch.setattr("app.domains.notifications.channels.send_email", _fails)

        with pytest.raises(EmailDeliveryError):
            await EmailChannel().deliver(
                db,
                recipient=Recipient(user_id=uuid.uuid4(), email="x@example.com"),
                message=RenderedNotification("item.created", "T", "B", {}),
            )

    async def test_the_email_channel_skips_a_scrubbed_address(
        self, db: AsyncSession
    ) -> None:
        """A deleted account has no address, and retrying cannot bring one
        back — so this must not raise and provoke a retry loop."""
        await EmailChannel().deliver(
            db,
            recipient=Recipient(user_id=uuid.uuid4(), email=None),
            message=RenderedNotification("item.created", "T", "B", {}),
        )


class TestIdempotency:
    """Delivery is at-least-once, so redelivery is normal, not exceptional.

    Worth knowing when changing these: the ``already_delivered`` /
    ``get_by_source_message`` lookups in the channels are **advisory only**.
    Removing them changes nothing observable, because the ``IntegrityError``
    handler catches the constraint violation and treats it as success — which is
    the correct design, since two workers can both read "not yet delivered"
    before either writes.

    The real mechanisms are the UNIQUE constraints on
    ``notifications.source_message_id`` and ``delivery_receipts``. Verified by
    removing each in turn: these tests go red, the pre-check ones do not.
    """

    async def test_redelivering_produces_one_notification(
        self, db: AsyncSession
    ) -> None:
        """The UNIQUE constraint on ``source_message_id`` is the mechanism; this
        proves it is actually wired to the channel."""
        user_id = uuid.uuid4()
        message_id = uuid.uuid4()
        channel = InAppChannel()
        message = RenderedNotification("item.created", "T", "B", {})

        for _ in range(3):
            await channel.deliver(
                db,
                recipient=Recipient(user_id=user_id),
                message=message,
                source_message_id=message_id,
            )

        assert await notifications_service.unread_count(db, user_id) == 1

    async def test_redelivering_writes_one_receipt(
        self, db: AsyncSession, monkeypatch
    ) -> None:
        """The email dedupe. Without it a redelivered password-reset notice
        arrives twice."""
        calls: list[str] = []

        async def _capture(*, to: str, subject: str, body: str) -> bool:
            calls.append(to)
            return True

        monkeypatch.setattr("app.domains.notifications.channels.send_email", _capture)
        message_id = uuid.uuid4()
        for _ in range(3):
            await EmailChannel().deliver(
                db,
                recipient=Recipient(user_id=uuid.uuid4(), email="x@example.com"),
                message=RenderedNotification("item.created", "T", "B", {}),
                source_message_id=message_id,
            )

        assert len(calls) == 1, "the message was sent more than once"
        receipts = (await db.execute(select(DeliveryReceipt))).scalars().all()
        assert len(receipts) == 1


class TestTheHandlerIsWiredUp:
    async def test_an_event_becomes_a_notification(self, db: AsyncSession) -> None:
        user_id = uuid.uuid4()

        channels = await handlers.deliver_event_on(
            db,
            Event(name="item.created", payload={"owner_id": str(user_id)}),
            message_id=uuid.uuid4(),
        )

        assert ChannelName.IN_APP in channels
        assert await notifications_service.unread_count(db, user_id) == 1

    async def test_an_event_without_a_recipient_is_skipped(
        self, db: AsyncSession
    ) -> None:
        """A payload with no user is an emitter bug, not a delivery failure —
        retrying it forever would fill the backlog."""
        channels = await handlers.deliver_event_on(
            db, Event(name="item.created", payload={}), message_id=uuid.uuid4()
        )

        assert channels == ()

    async def test_the_outbox_carries_the_event_to_the_handler(
        self, db: AsyncSession
    ) -> None:
        """End to end: staged durably, claimed, delivered."""
        from app import worker
        from app.domains.notifications.subscribers import (
            register_notification_subscribers,
        )

        register_notification_subscribers()
        assert worker.handlers_for("item.created")

        user_id = uuid.uuid4()
        publish_transactional(
            db, Event(name="item.created", payload={"owner_id": str(user_id)})
        )
        await db.commit()

        # The registered handler opens its own session against the application
        # engine, which this suite cannot reach — so the batch is driven with the
        # session-taking half substituted in. What is proven is the whole chain:
        # staged durably, claimed by the relay, delivered to a real notification.
        async def _handle(event: Event, *, message_id: uuid.UUID) -> None:
            await handlers.deliver_event_on(db, event, message_id=message_id)

        worker._HANDLERS["item.created"] = [_handle]
        try:
            assert await worker.process_batch(db) == 1
        finally:
            worker._HANDLERS.clear()

        assert await notifications_service.unread_count(db, user_id) == 1

    def test_every_notified_event_has_a_handler(self) -> None:
        """A type in NOTIFICATION_TYPES that nothing subscribes to reads as
        supported while notifying nobody."""
        from app import worker
        from app.domains.notifications.subscribers import (
            register_notification_subscribers,
        )

        register_notification_subscribers()
        for name in NOTIFICATION_TYPES:
            assert worker.handlers_for(name), f"{name} has no handler"


class TestNotificationEndpoints:
    async def test_listing_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/notifications")).status_code == 401

    async def test_unread_count_requires_authentication(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/api/v1/notifications/unread-count")
        assert response.status_code == 401

    async def test_lists_the_callers_notifications(
        self,
        client: AsyncClient,
        db: AsyncSession,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
    ) -> None:
        await _notify(db, uuid.UUID(registered_user["id"]))

        response = await client.get("/api/v1/notifications", headers=auth_headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert {"items", "total", "limit", "offset"} <= set(body)
        assert body["total"] == 1

    async def test_does_not_leak_another_users_notifications(
        self, client: AsyncClient, db: AsyncSession, auth_headers: dict[str, str]
    ) -> None:
        """The leak that returns a healthy 200 and looks completely normal."""
        await _notify(db, uuid.uuid4())

        response = await client.get("/api/v1/notifications", headers=auth_headers)

        assert response.json()["total"] == 0

    async def test_reports_the_unread_count(
        self,
        client: AsyncClient,
        db: AsyncSession,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
    ) -> None:
        await _notify(db, uuid.UUID(registered_user["id"]))

        response = await client.get(
            "/api/v1/notifications/unread-count", headers=auth_headers
        )

        assert response.status_code == 200, response.text
        assert response.json() == {"unread": 1}

    async def test_marking_read_clears_the_badge(
        self,
        client: AsyncClient,
        db: AsyncSession,
        auth_headers: dict[str, str],
        registered_user: dict[str, str],
    ) -> None:
        notification = await _notify(db, uuid.UUID(registered_user["id"]))

        marked = await client.post(
            f"/api/v1/notifications/{notification.id}/read", headers=auth_headers
        )
        assert marked.status_code == 200, marked.text

        count = await client.get(
            "/api/v1/notifications/unread-count", headers=auth_headers
        )
        assert count.json() == {"unread": 0}

    async def test_marking_another_users_notification_is_404(
        self, client: AsyncClient, db: AsyncSession, auth_headers: dict[str, str]
    ) -> None:
        theirs = await _notify(db, uuid.uuid4())

        response = await client.post(
            f"/api/v1/notifications/{theirs.id}/read", headers=auth_headers
        )

        assert response.status_code == 404, response.text

    async def test_read_all_is_not_captured_as_an_id(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """``/read-all`` is declared before ``/{id}/read``; if the order were
        reversed it would be parsed as a UUID and answer 422."""
        response = await client.post(
            "/api/v1/notifications/read-all", headers=auth_headers
        )

        assert response.status_code == 204, response.text

    async def test_preferences_round_trip(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        update = await client.put(
            "/api/v1/notifications/preferences",
            json={
                "notification_type": "item.created",
                "channel": "in_app",
                "enabled": False,
            },
            headers=auth_headers,
        )
        assert update.status_code == 204, update.text

        prefs = await client.get(
            "/api/v1/notifications/preferences", headers=auth_headers
        )
        entry = next(
            p
            for p in prefs.json()
            if p["notification_type"] == "item.created" and p["channel"] == "in_app"
        )
        assert entry["enabled"] is False

    async def test_silencing_a_security_notice_is_refused(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        response = await client.put(
            "/api/v1/notifications/preferences",
            json={
                "notification_type": "user.deactivated",
                "channel": "email",
                "enabled": False,
            },
            headers=auth_headers,
        )

        assert response.status_code == 422, response.text

    async def test_there_is_no_way_to_create_a_notification(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """An endpoint that accepted one would let any caller forge a message
        that appears to come from the platform."""
        response = await client.post(
            "/api/v1/notifications",
            json={"user_id": str(uuid.uuid4()), "title": "Forged", "body": "x"},
            headers=auth_headers,
        )

        assert response.status_code == 405, response.text
