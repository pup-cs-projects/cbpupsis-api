"""Pydantic schemas (DTOs) for the notifications domain.

There is no ``NotificationCreate``: nothing outside this application may create
a notification, because an endpoint that accepted one would let any caller forge
a message that appears to come from the platform. Notifications arrive through
the outbox, never from a client.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domains.notifications.constants import TYPE_MAX_LENGTH, Channel


class NotificationRead(BaseModel):
    """One notification as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    notification_type: str
    title: str
    body: str
    payload: dict[str, Any]
    read_at: datetime | None
    created_at: datetime


class UnreadCount(BaseModel):
    """The bell badge.

    An object rather than a bare integer so the response can gain a field — a
    highest-unread id, a category breakdown — without breaking every client that
    parses it.
    """

    unread: int


class NotificationPreferenceRead(BaseModel):
    """One resolved preference: what will actually happen for this type.

    Returned for **every** known type, whether or not the user has stored a row,
    because a client rendering a settings screen needs the effective state — not
    just the overrides.
    """

    notification_type: str
    channel: Channel
    enabled: bool
    #: Whether the user may change it. False for security notices, so a client
    #: can render the toggle disabled rather than letting the user try and be
    #: refused.
    editable: bool


class NotificationPreferenceUpdate(BaseModel):
    """Payload for changing one preference."""

    notification_type: str = Field(..., max_length=TYPE_MAX_LENGTH)
    channel: Channel
    enabled: bool
