"""One-time tokens and email verification.

Verification links are mailed through the outbox (``delivery.py``), never
inline, so a link is not lost when the process stops after the response.

Mailed tokens are cryptographically random, expiring, and single use.
Verification uses :func:`consume_one_time_token`; reset consumes its token in
the same transaction as the password change. Verification shares these errors:

- **Single use.** Redemption stamps ``consumed_at`` in the same transaction that
  acts on it, so a token replayed from a browser history or a mail forward fails.
- **Expiring.** Past ``expires_at`` a token is dead regardless of use.
- **Indistinguishable failures.** Unknown, expired, already-used, and
  wrong-purpose all raise one identical error. Reporting which would tell an
  attacker holding a guessed value whether it was ever real.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_core.config import settings
from cbpupsis_core.events import Event
from cbpupsis_database.models.auth import TokenPurpose
from cbpupsis_shared.domains.auth import repository
from cbpupsis_shared.domains.auth.exceptions import InvalidTokenError
from cbpupsis_shared.domains.auth.security import (
    VERIFICATION_EMAIL,
    encrypt_mailed_token,
    generate_one_time_token,
    hash_one_time_token,
)
from cbpupsis_shared.domains.users import service as users_service
from cbpupsis_shared.outbox import publish_transactional

logger = logging.getLogger(__name__)


async def invalidate_outstanding(
    db: AsyncSession, user_id: uuid.UUID, purpose: TokenPurpose
) -> None:
    """Consume every live token a user holds for one purpose."""
    await repository.consume_outstanding_one_time_tokens(
        db, user_id, purpose, datetime.now(UTC)
    )


async def consume_one_time_token(
    db: AsyncSession, raw_token: str, purpose: TokenPurpose
) -> uuid.UUID:
    """Redeem a mailed token, returning the user id it belongs to.

    Raises :class:`UnauthorizedError` with one identical message for every
    failure mode: unknown, wrong purpose, expired, or already consumed.

    The row is marked consumed via a conditional UPDATE rather than a
    read-then-write: ``WHERE consumed_at IS NULL`` makes the database the
    arbiter, so two requests racing with the same token produce one winner. A
    check-then-set here would let both pass the check before either wrote, and
    the token would be used twice.
    """
    token_hash = hash_one_time_token(raw_token)
    now = datetime.now(UTC)

    user_id = await repository.consume_one_time_token(db, token_hash, purpose, now)
    if user_id is None:
        await db.rollback()
        raise InvalidTokenError

    await db.commit()
    return user_id


async def stage_verification_email(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Stage a fresh verification token and its mail job. Does not commit.

    Outstanding verification tokens are spent first, so only the newest link
    works. The job commits with the caller's transaction and the worker mails
    it, so the link survives a crash or a frozen Lambda after the response.
    Only the digest is stored; the raw token travels encrypted in the job.
    """
    await invalidate_outstanding(db, user_id, TokenPurpose.email_verification)
    raw_token, token_hash = generate_one_time_token()
    repository.add_one_time_token(
        db,
        user_id=user_id,
        token_hash=token_hash,
        purpose=TokenPurpose.email_verification,
        expires_at=datetime.now(UTC)
        + timedelta(hours=settings.email_verification_ttl_hours),
    )
    publish_transactional(
        db,
        Event(
            name="auth.verification_email",
            payload={
                "user_id": str(user_id),
                "token_hash": token_hash,
                "encrypted_token": encrypt_mailed_token(
                    raw_token,
                    label=VERIFICATION_EMAIL,
                    user_id=user_id,
                    token_hash=token_hash,
                ),
            },
        ),
    )


async def verify_email(db: AsyncSession, raw_token: str) -> None:
    """Redeem a verification token and stamp the user as verified.

    Raises :class:`UnauthorizedError` on any invalid token. A token whose user
    has since been deleted fails the same way, so a deletion cannot be detected
    by watching this endpoint's response change.
    """
    user_id = await consume_one_time_token(
        db, raw_token, TokenPurpose.email_verification
    )
    user = await users_service.get_active_user(db, user_id)
    if user is None:
        raise InvalidTokenError
    await users_service.mark_email_verified(db, user_id)


async def resend_verification(db: AsyncSession, email: str) -> None:
    """Send a fresh verification email, if that is a meaningful thing to do.

    Returns ``None`` unconditionally. An unknown address, an inactive account,
    and an already-verified one all take the same silent path: reporting the
    difference would turn this endpoint into a registration oracle, which is the
    exact thing the identical login error exists to prevent.
    """
    user = await users_service.get_by_email(db, email.lower())
    if user is None or not user.is_active or user.email_verified_at is not None:
        logger.info("Verification resend requested for a non-eligible address")
        return
    await stage_verification_email(db, user.id)
    await db.commit()
