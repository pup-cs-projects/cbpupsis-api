"""Private Google credentials, OAuth attempts and durable reconciliation state."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from cbpupsis_database.base import Base


class GoogleCalendarAccount(Base):
    __tablename__ = "google_calendar_accounts"

    student_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    generation: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4)
    subject_hash: Mapped[str | None] = mapped_column(String(64))
    refresh_cipher: Mapped[str | None] = mapped_column(Text)
    revoke_cipher: Mapped[str | None] = mapped_column(Text)
    targets: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    next_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_id: Mapped[uuid.UUID | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GoogleCalendarOAuthState(Base):
    __tablename__ = "google_calendar_oauth_states"

    student_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    session_id: Mapped[uuid.UUID] = mapped_column()
    payload_cipher: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
