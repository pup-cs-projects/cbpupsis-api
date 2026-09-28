"""Data access for the auth domain: the refresh-token and one-time-token ledgers.

Two of these functions carry a security property in their *shape*, not just in
their SQL, so they are worth reading before changing:

- :func:`consume_one_time_token` marks a token used with a conditional UPDATE
  (``WHERE consumed_at IS NULL AND expires_at > now``) and returns the owning
  user id via RETURNING. Read-then-write would let two requests racing with the
  same token both pass the check before either wrote, and the token would be
  redeemed twice. Keeping the predicate and the write in one statement makes the
  database the arbiter, so exactly one caller wins.
- :func:`revoke_refresh_token` and :func:`revoke_all_refresh_tokens_for_user`
  filter on ``revoked_at IS NULL`` so a re-revocation cannot overwrite the
  original timestamp, which is the audit record of when a token actually died.

Nothing here commits, and that matters more than usual in this domain: whether a
consumed token stays consumed when a later rule fails is a transaction decision,
and it belongs to the service that knows which rule ran. See
``service._consume_one_time_token``, which rolls back on a miss and commits on a
hit, and ``service.refresh``, which deliberately commits a rotation *before* the
email-verification gate can raise so a blocked account cannot retry the token.

Returning ORM rows and scalars, never schemas or domain errors: a lookup that
finds nothing returns ``None``, and deciding that this means "invalid or expired
token" — with one identical message for every failure mode — is the service's job.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cbpupsis_database.models.auth import OneTimeToken, RefreshToken, TokenPurpose

# --------------------------------------------------------------------------- #
# Refresh tokens
# --------------------------------------------------------------------------- #


async def get_refresh_token(db: AsyncSession, jti: str) -> RefreshToken | None:
    """Return the refresh-token record with this ``jti``, or ``None``.

    Deliberately does not filter on ``revoked_at``: the caller needs to tell
    "no such token" from "a token that was already used", because the second is
    evidence of replay and triggers revocation of the whole family. Filtering
    revoked rows out here would erase that distinction.

    SQL::

        SELECT refresh_tokens.user_id, refresh_tokens.jti,
               refresh_tokens.expires_at, refresh_tokens.revoked_at,
               refresh_tokens.id, refresh_tokens.created_at,
               refresh_tokens.updated_at
        FROM refresh_tokens
        WHERE refresh_tokens.jti = :jti_1
    """
    return await db.scalar(select(RefreshToken).where(RefreshToken.jti == jti))


def add_refresh_token(
    db: AsyncSession, user_id: uuid.UUID, jti: str, expires_at: datetime
) -> RefreshToken:
    """Stage a newly issued refresh token on the session and return it.

    Only the ``jti`` is recorded, never the token string: knowing the id is
    enough to revoke, and storing the credential itself would turn a database
    leak into a set of usable sessions.

    Emits no SQL here. On the service's commit::

        INSERT INTO refresh_tokens (user_id, jti, expires_at, revoked_at, id)
        VALUES (:user_id::UUID, :jti, :expires_at, :revoked_at, :id::UUID)
        RETURNING refresh_tokens.created_at, refresh_tokens.updated_at
    """
    record = RefreshToken(user_id=user_id, jti=jti, expires_at=expires_at)
    db.add(record)
    return record


def mark_refresh_token_revoked(
    db: AsyncSession, record: RefreshToken, revoked_at: datetime
) -> RefreshToken:
    """Stamp ``revoked_at`` on an already-loaded refresh-token row.

    Distinct from :func:`revoke_refresh_token`, which revokes by ``jti`` in one
    statement. Rotation has already loaded the row to inspect ``revoked_at`` for
    replay detection, so it mutates that instance instead of issuing a second
    lookup — and it must stay the *same* instance, because the caller flushes
    and commits around it at a precisely chosen point.

    Does not flush: when the write lands is the service's decision, and in the
    rotation path that timing is a security property.

    Emits no SQL here. On the service's flush or commit — note the unit of work
    keys on the primary key, not on ``jti``, unlike
    :func:`revoke_refresh_token`::

        UPDATE refresh_tokens
        SET revoked_at=:revoked_at, updated_at=now()
        WHERE refresh_tokens.id = :id_1::UUID
    """
    record.revoked_at = revoked_at
    return record


async def revoke_refresh_token(
    db: AsyncSession, jti: str, revoked_at: datetime
) -> None:
    """Mark one live refresh token revoked. A no-op if it is already revoked.

    ``WHERE revoked_at IS NULL`` keeps the first revocation's timestamp intact,
    so the ledger records when the token really stopped working.

    SQL::

        UPDATE refresh_tokens
        SET revoked_at=:revoked_at, updated_at=now()
        WHERE refresh_tokens.jti = :jti_1
          AND refresh_tokens.revoked_at IS NULL
    """
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.jti == jti, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=revoked_at)
    )


async def revoke_all_refresh_tokens_for_user(
    db: AsyncSession, user_id: uuid.UUID, revoked_at: datetime
) -> None:
    """Revoke every live refresh token a user holds.

    A set-based UPDATE rather than a loop over loaded rows: it is one round trip
    regardless of how many sessions exist, and it cannot race with a token
    issued between a SELECT and a write.

    SQL::

        UPDATE refresh_tokens
        SET revoked_at=:revoked_at, updated_at=now()
        WHERE refresh_tokens.user_id = :user_id_1::UUID
          AND refresh_tokens.revoked_at IS NULL
    """
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=revoked_at)
    )


# --------------------------------------------------------------------------- #
# One-time tokens (email verification, password reset)
# --------------------------------------------------------------------------- #


def add_one_time_token(
    db: AsyncSession,
    user_id: uuid.UUID,
    token_hash: str,
    purpose: TokenPurpose,
    expires_at: datetime,
) -> OneTimeToken:
    """Stage a one-time token on the session and return it.

    Takes the digest, never the raw secret — the raw value exists only in the
    email that carries it, so a database leak yields nothing replayable.

    Emits no SQL here. On the service's commit::

        INSERT INTO one_time_tokens (user_id, token_hash, purpose, expires_at,
                                     consumed_at, id)
        VALUES (:user_id::UUID, :token_hash, :purpose, :expires_at,
                :consumed_at, :id::UUID)
        RETURNING one_time_tokens.created_at, one_time_tokens.updated_at
    """
    record = OneTimeToken(
        user_id=user_id,
        token_hash=token_hash,
        purpose=purpose,
        expires_at=expires_at,
    )
    db.add(record)
    return record


async def consume_outstanding_one_time_tokens(
    db: AsyncSession,
    user_id: uuid.UUID,
    purpose: TokenPurpose,
    consumed_at: datetime,
) -> None:
    """Mark every live token a user holds for one purpose as consumed.

    Called before issuing a replacement, so a "resend" invalidates the previous
    link instead of leaving a second live credential sitting in another inbox.

    SQL::

        UPDATE one_time_tokens
        SET consumed_at=:consumed_at, updated_at=now()
        WHERE one_time_tokens.user_id = :user_id_1::UUID
          AND one_time_tokens.purpose = :purpose_1
          AND one_time_tokens.consumed_at IS NULL
    """
    await db.execute(
        update(OneTimeToken)
        .where(
            OneTimeToken.user_id == user_id,
            OneTimeToken.purpose == purpose,
            OneTimeToken.consumed_at.is_(None),
        )
        .values(consumed_at=consumed_at)
    )


async def consume_one_time_token(
    db: AsyncSession,
    token_hash: str,
    purpose: TokenPurpose,
    now: datetime,
) -> uuid.UUID | None:
    """Atomically redeem a one-time token, returning its user id or ``None``.

    ``None`` covers every failure mode without distinguishing them — unknown
    digest, wrong purpose, expired, already consumed. That is intentional: the
    service maps them all to one identical error message, and a repository that
    reported *which* check failed would make it possible to reintroduce an
    oracle simply by logging the reason.

    The predicate and the write are one statement (see the module docstring):
    the row is only stamped if it is still unconsumed and unexpired at the
    instant of the UPDATE, so concurrent redemptions produce exactly one winner.

    SQL::

        UPDATE one_time_tokens
        SET consumed_at=:consumed_at, updated_at=now()
        WHERE one_time_tokens.token_hash = :token_hash_1
          AND one_time_tokens.purpose = :purpose_1
          AND one_time_tokens.consumed_at IS NULL
          AND one_time_tokens.expires_at > :expires_at_1
        RETURNING one_time_tokens.user_id
    """
    result = await db.execute(
        update(OneTimeToken)
        .where(
            OneTimeToken.token_hash == token_hash,
            OneTimeToken.purpose == purpose,
            OneTimeToken.consumed_at.is_(None),
            OneTimeToken.expires_at > now,
        )
        .values(consumed_at=now)
        .returning(OneTimeToken.user_id)
    )
    return result.scalar_one_or_none()
