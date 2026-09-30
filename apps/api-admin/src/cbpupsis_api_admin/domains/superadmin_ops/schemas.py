"""Contracts for explicit overrides and restore authorization."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: uuid.UUID
    justification: str | None = Field(default=None, max_length=1000)


class OverrideResult(BaseModel):
    rule_id: str
    target_type: str
    target_id: uuid.UUID
    status: Literal["applied"] = "applied"


class RestoreAuthorizationRead(BaseModel):
    backup_id: uuid.UUID
    status: Literal["pending", "approved"]
    requested_by: uuid.UUID
    approved_by: uuid.UUID | None = None
    requested_at: datetime
