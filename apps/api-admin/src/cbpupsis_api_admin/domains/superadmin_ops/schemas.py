"""Superadmin operation request and response contracts."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class OverrideRequest(BaseModel):
    target_id: uuid.UUID
    justification: str | None = Field(default=None, max_length=2000)


class OverrideResult(BaseModel):
    rule_id: str
    target_id: uuid.UUID
    result: str


class RestoreRequestRead(BaseModel):
    backup_id: uuid.UUID
    status: str
    requested_by: uuid.UUID
    approved_by: uuid.UUID | None
    requested_at: datetime
