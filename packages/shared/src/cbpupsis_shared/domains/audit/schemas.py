"""Pydantic schemas (DTOs) for the audit domain.

Read-only: there is no ``Create`` schema, because nothing outside the event
subscriber may write an audit entry. An API that accepted hand-written audit
rows would undermine the only property the table has.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, IPvAnyAddress, field_serializer


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
        return render_in_utc8(value)


class SignInAttemptRead(BaseModel):
    """One recorded sign-in attempt from any role's sign-in flow."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    attempted_id: str
    user_id: uuid.UUID | None
    ip_address: IPvAnyAddress | None
    user_agent: str | None
    success: bool
    failure_reason: str | None
    mfa_used: bool
    created_at: datetime

    @field_serializer("created_at")
    def serialize_created_at(self, value: datetime) -> str:
        """Same rendering as :class:`AuditEntryRead`."""
        return render_in_utc8(value)


def render_in_utc8(value: datetime) -> str:
    """Render a stored UTC instant as ISO 8601 in UTC+8; a naive value is UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(timezone(timedelta(hours=8))).isoformat()


class StudentRecentActivityRead(BaseModel):
    """A recent sign-in or security audit event shown on the dashboard."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    occurred_at: str
    type: str
    severity: str
    description: str
