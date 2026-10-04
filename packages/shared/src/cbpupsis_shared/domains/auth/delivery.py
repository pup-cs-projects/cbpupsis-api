"""Durable reset-link delivery. Retry the same token, never issue another."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core import emails
from cbpupsis_core.emails import send_email
from cbpupsis_core.events import Event
from cbpupsis_database.models.auth import TokenPurpose
from cbpupsis_database.session import AsyncSessionLocal
from cbpupsis_shared.domains.auth import repository
from cbpupsis_shared.domains.auth.security import (
    decrypt_reset_token,
    hash_one_time_token,
)
from cbpupsis_shared.domains.users import service as users_service
from cbpupsis_shared.outbox import add_receipt, already_delivered, lock_message


class ResetEmailDeliveryError(RuntimeError):
    """Opaque failure safe to store in the outbox's last_error column."""


async def deliver_reset_email(event: Event, *, message_id: uuid.UUID) -> None:
    """Worker entry point; request sessions are never reused in production."""
    async with AsyncSessionLocal() as db:
        await deliver_reset_email_on(db, event, message_id=message_id)


async def deliver_reset_email_on(
    db: AsyncSession, event: Event, *, message_id: uuid.UUID
) -> None:
    """Check the receipt, deliver a usable link, and commit its receipt."""
    await lock_message(db, message_id)
    if await already_delivered(db, message_id, "reset_email"):
        await db.commit()
        return
    user_id = uuid.UUID(event.payload["user_id"])
    token_hash = event.payload["token_hash"]
    record = await repository.get_one_time_token(
        db, token_hash, TokenPurpose.password_reset
    )
    user = await users_service.get_active_user(db, user_id)
    expires_at = record.expires_at if record is not None else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if (
        record is not None
        and record.user_id == user_id
        and record.consumed_at is None
        and expires_at > datetime.now(UTC)
        and user is not None
    ):
        try:
            raw_token = decrypt_reset_token(
                event.payload["encrypted_token"], user_id=user_id, token_hash=token_hash
            )
            if hash_one_time_token(raw_token) != token_hash:
                raise ResetEmailDeliveryError("Invalid reset delivery payload")
            subject, body = emails.password_reset_email(
                raw_token, expires_at=expires_at
            )
            delivered = await send_email(to=user.email, subject=subject, body=body)
        except Exception:
            raise ResetEmailDeliveryError(
                "Password reset email delivery failed"
            ) from None
        if not delivered:
            raise ResetEmailDeliveryError("Password reset email delivery failed")
    # Expired, spent, or deleted accounts are terminal skips, not retry loops.
    add_receipt(db, message_id, "reset_email")
    await db.commit()
