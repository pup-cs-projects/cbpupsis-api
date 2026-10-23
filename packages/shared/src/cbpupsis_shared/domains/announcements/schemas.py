"""Schemas for the announcements domain."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AnnouncementRead(BaseModel):
    id: str
    title: str
    body: str
    category: str = Field("ACADEMIC")
    published_at: datetime = Field(..., alias="publishedAt")
    expires_at: datetime | None = Field(None, alias="expiresAt")
    is_pinned: bool = Field(False, alias="isPinned")

    model_config = ConfigDict(
        from_attributes=True, populate_by_name=True, serialize_by_alias=True
    )
