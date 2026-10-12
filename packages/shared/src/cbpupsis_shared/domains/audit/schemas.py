"""Pydantic schemas (DTOs) for the audit domain.

Read-only: there is no ``Create`` schema, because nothing outside the event
subscriber may write an audit entry. An API that accepted hand-written audit
rows would undermine the only property the table has.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer


class AuditEntryRead(BaseModel):
    """One recorded administrative change, as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    action: str
    actor_id: uuid.UUID | None
    target_type: str | None
    target_id: str | None
    payload: dict[str, Any]
    prior_state: dict[str, Any] | None
    new_state: dict[str, Any] | None
    occurred_at: datetime

    @field_serializer("occurred_at")
    def serialize_occurred_at(self, value: datetime) -> str:
        """Store UTC instants, render the audit contract in UTC+8."""
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(timezone(timedelta(hours=8))).isoformat()
