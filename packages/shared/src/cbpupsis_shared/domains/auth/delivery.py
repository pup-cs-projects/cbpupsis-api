"""Durable delivery of mailed secrets. Retry the same one, never issue another.

Reset and verification links take the same path: staged with their token in the
transaction that issued it, decrypted and mailed by the outbox worker, and
receipted so a retried message never mails twice. A new account's temporary
password travels the same way, encrypted under its own label.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core import emails
from cbpupsis_core.emails import send_email
from cbpupsis_core.events import Event
from cbpupsis_database.models.auth import TokenPurpose
from cbpupsis_database.session import AsyncSessionLocal
from cbpupsis_shared.domains.auth import repository
from cbpupsis_shared.domains.auth import service as auth_service
from cbpupsis_shared.domains.auth.security import (
    RESET_EMAIL,
    TEMPORARY_PASSWORD_EMAIL,
    VERIFICATION_EMAIL,
    decrypt_mailed_token,
    hash_one_time_token,
    verify_password,
)
from cbpupsis_shared.domains.users import service as users_service
from cbpupsis_shared.outbox import add_receipt, already_delivered, lock_message


class ResetEmailDeliveryError(RuntimeError):
    """Opaque failure safe to store in the outbox's last_error column."""


class VerificationEmailDeliveryError(RuntimeError):
    """Opaque failure safe to store in the outbox's last_error column."""


class TemporaryPasswordEmailDeliveryError(RuntimeError):
    """Opaque failure safe to store in the outbox's last_error column."""


async def deliver_reset_email(event: Event, *, message_id: uuid.UUID) -> None:
    """Worker entry point; request sessions are never reused in production."""
    async with AsyncSessionLocal() as db:
        await deliver_reset_email_on(db, event, message_id=message_id)


async def deliver_reset_email_on(
    db: AsyncSession, event: Event, *, message_id: uuid.UUID
) -> None:
    """Check the receipt, deliver a usable reset link, and commit its receipt."""
    await deliver_mailed_link(
        db,
        event,
        message_id=message_id,
        purpose=TokenPurpose.password_reset,
        label=RESET_EMAIL,
        receipt="reset_email",
        render=lambda token, expires_at, user, role: emails.password_reset_email(
            token, role=role, expires_at=expires_at
        ),
        error=ResetEmailDeliveryError("Password reset email delivery failed"),
    )


async def deliver_verification_email(event: Event, *, message_id: uuid.UUID) -> None:
    """Worker entry point; request sessions are never reused in production."""
    async with AsyncSessionLocal() as db:
        await deliver_verification_email_on(db, event, message_id=message_id)


async def deliver_verification_email_on(
    db: AsyncSession, event: Event, *, message_id: uuid.UUID
) -> None:
    """Deliver a usable verification link to a still-unverified account."""
    await deliver_mailed_link(
        db,
        event,
        message_id=message_id,
        purpose=TokenPurpose.email_verification,
        label=VERIFICATION_EMAIL,
        receipt="verification_email",
        render=lambda token, expires_at, user, role: (
            None
            if user.email_verified_at is not None
            else emails.verification_email(token, role=role)
        ),
        error=VerificationEmailDeliveryError("Verification email delivery failed"),
    )


async def deliver_mailed_link(
    db: AsyncSession,
    event: Event,
    *,
    message_id: uuid.UUID,
    purpose: TokenPurpose,
    label: str,
    receipt: str,
    render: Callable[[str, datetime, object, str | None], tuple[str, str] | None],
    error: RuntimeError,
) -> None:
    """Mail a live token's link once; a dead token or account is a terminal skip.

    ``render`` returns ``None`` to skip a link that no longer helps (an address
    verified since). It is given the account's portal role, so the link opens
    the portal that account signs in to. Failures raise ``error`` without the
    original exception, because its text lands in ``last_error`` and must never
    carry the token.
    """
    await lock_message(db, message_id)
    if await already_delivered(db, message_id, receipt):
        await db.commit()
        return
    user_id = uuid.UUID(event.payload["user_id"])
    token_hash = event.payload["token_hash"]
    record = await repository.get_one_time_token(db, token_hash, purpose)
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
        role = await auth_service.portal_role(db, user_id)
        try:
            raw_token = decrypt_mailed_token(
                event.payload["encrypted_token"],
                label=label,
                user_id=user_id,
                token_hash=token_hash,
            )
            if hash_one_time_token(raw_token) != token_hash:
                raise error
            message = render(raw_token, expires_at, user, role)
            delivered = (
                True
                if message is None
                else await send_email(
                    to=user.email, subject=message[0], body=message[1]
                )
            )
        except Exception:
            raise error from None
        if not delivered:
            raise error
    # Expired, spent, or deleted accounts are terminal skips, not retry loops.
    add_receipt(db, message_id, receipt)
    await db.commit()


async def deliver_temporary_password_email(
    event: Event, *, message_id: uuid.UUID
) -> None:
    """Worker entry point; request sessions are never reused in production."""
    async with AsyncSessionLocal() as db:
        await deliver_temporary_password_email_on(db, event, message_id=message_id)


async def deliver_temporary_password_email_on(
    db: AsyncSession, event: Event, *, message_id: uuid.UUID
) -> None:
    """Mail a temporary password once, while it is still the one that works.

    A terminal skip when the account is gone, the user has already chosen a
    password, the password expired, or a reissue replaced it (the decrypted
    password no longer matches the stored digest). Failures raise an opaque
    error, because the text lands in ``last_error`` and must never carry it.
    """
    await lock_message(db, message_id)
    receipt = "temporary_password_email"
    if await already_delivered(db, message_id, receipt):
        await db.commit()
        return
    error = TemporaryPasswordEmailDeliveryError(
        "Temporary password email delivery failed"
    )
    user_id = uuid.UUID(event.payload["user_id"])
    expires_at = datetime.fromisoformat(event.payload["expires_at"])
    user = await users_service.get_active_user(db, user_id)
    if (
        user is not None
        and user.password_change_required
        and expires_at > datetime.now(UTC)
    ):
        role = await auth_service.portal_role(db, user_id)
        try:
            raw_password = decrypt_mailed_token(
                event.payload["encrypted_password"],
                label=TEMPORARY_PASSWORD_EMAIL,
                user_id=user_id,
                token_hash=event.payload["issue_id"],
            )
            current = await asyncio.to_thread(
                verify_password, raw_password, user.password_hash
            )
            delivered = not current or await send_email(
                to=user.email,
                subject=emails.WELCOME_SUBJECT,
                body=emails.welcome_email(
                    raw_password, role=role, expires_at=expires_at
                )[1],
            )
        except Exception:
            raise error from None
        if not delivered:
            raise error
    add_receipt(db, message_id, receipt)
    await db.commit()
