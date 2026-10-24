"""Pydantic schemas (DTOs) for the announcements domain."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AnnouncementRead(BaseModel):
    """An institution-wide or academic announcement."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    body: str
    category: str = Field(default="ACADEMIC")
    published_at: datetime
    expires_at: datetime | None = None
    is_pinned: bool = False
