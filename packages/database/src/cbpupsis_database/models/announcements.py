"""SQLAlchemy models for campus announcements domain."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from cbpupsis_database.base import Base, UUIDMixin


class Announcement(UUIDMixin, Base):
    """Institutional announcements targeted to specific roles or global."""

    __tablename__ = "announcements"

    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(30), server_default="GENERAL")
    banner_image_url: Mapped[str | None] = mapped_column(Text, default=None)
    target_role_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("roles.id", ondelete="SET NULL"), default=None
    )
    is_pinned: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    is_published: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )

    __table_args__ = (
        Index(
            "ix_announcements_published_pinned",
            "is_published",
            "is_pinned",
            "published_at",
        ),
    )
